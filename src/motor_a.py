"""
Motor A — File synchronization engine.

Strategy:
  1. Use rsync --dry-run to compute delta (new / changed / deleted files).
  2. Show delta in UI and wait for confirmation.
  3. On confirm: relay files through the local machine as proxy (old→local temp dir→new).
     Direct server↔server transfer is assumed to be unavailable.

Old server is read-only — rsync only reads from it, never writes.
Deletion on new server requires a separate second confirmation + DB backup guard.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .config import SyncConfig
from .ssh_wrapper import OldServer, NewServer
from .state_db import StateDB

logger = logging.getLogger(__name__)


@dataclass
class FileChange:
    relative_path: str
    change_type: str   # 'new' | 'modified' | 'deleted'
    size_bytes: int = 0


@dataclass
class FileDelta:
    sync_dir_source: str
    sync_dir_dest: str
    new_files: List[FileChange] = field(default_factory=list)
    modified_files: List[FileChange] = field(default_factory=list)
    deleted_files: List[FileChange] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return len(self.new_files) + len(self.modified_files)

    @property
    def delete_count(self) -> int:
        return len(self.deleted_files)


def _build_old_ssh_opts(old: OldServer) -> str:
    """Build -e 'ssh ...' string for rsync connecting to old server."""
    parts = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if old.ssh_key:
        parts.extend(["-i", str(Path(old.ssh_key).expanduser())])
    return " ".join(parts)


def _build_new_ssh_opts(new: NewServer) -> str:
    parts = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if new.ssh_key:
        parts.extend(["-i", str(Path(new.ssh_key).expanduser())])
    return " ".join(parts)


def _run_rsync_streaming(
    cmd: List[str], job_id: int, db, log_every: int = 25, timeout: int = 1800
) -> Tuple[int, str, str]:
    """Run rsync and log progress every log_every files via db.log()."""
    logger.debug("rsync streaming: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout_lines: List[str] = []
        count = 0
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            stdout_lines.append(line)
            if line and not line.startswith("sending") and not line.startswith("receiving"):
                count += 1
                if count % log_every == 0:
                    db.log(job_id, f"  ... {count} souborů zpracováno ({line[-60:]})", "INFO")
        proc.wait(timeout=timeout)
        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        return proc.returncode, "\n".join(stdout_lines), stderr
    except subprocess.TimeoutExpired:
        proc.kill()
        return 124, "", "rsync timed out"


def _run_rsync(cmd: List[str], timeout: int = 600) -> Tuple[int, str, str]:
    logger.debug("rsync: %s", " ".join(cmd))
    try:
        # Use bytes mode + errors='replace' to handle non-UTF-8 filenames
        # (old Czech servers may have Latin-2 encoded filenames)
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        stdout = r.stdout.decode("utf-8", errors="replace")
        stderr = r.stderr.decode("utf-8", errors="replace")
        return r.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        return 124, "", "rsync timed out"


def _parse_rsync_dry_run(stdout: str) -> Tuple[List[FileChange], List[FileChange], List[FileChange]]:
    """
    Parse rsync --dry-run --out-format='%i %''l %n' output.
    %i = item flags, %l = file size, %n = filename.
    Item flag starts with '>f' = file transfer, '*deleting' = deletion.
    """
    new_files = []
    modified_files = []
    deleted_files = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue

        # deletion lines look like: *deleting   path/to/file
        if line.startswith("*deleting"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                deleted_files.append(FileChange(parts[1], "deleted"))
            continue

        # Transfer lines: >f.st...... 12345 path/to/file
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        flags, size_str, path = parts
        if not flags.startswith(">f"):
            continue
        try:
            size = int(size_str)
        except ValueError:
            size = 0

        # >f+++++++++ = new file,  >f.st...... = modified (size/time changed)
        if "++++++++" in flags:
            new_files.append(FileChange(path, "new", size))
        else:
            modified_files.append(FileChange(path, "modified", size))

    return new_files, modified_files, deleted_files


class MotorA:
    def __init__(self, cfg: SyncConfig, old: OldServer, new_: NewServer, db: StateDB):
        self.cfg = cfg
        self.old = old
        self.new = new_
        self.db = db
        self.temp_dir = cfg.temp_dir

    # -- Delta detection via find (fast — 2 SSH calls instead of rsync scan) ---

    def _list_remote_files(self, ssh_obj, remote_path: str) -> Dict[str, Tuple[int, float]]:
        """
        Returns {relative_path: (size_bytes, mtime_epoch)} for all files
        under remote_path using a single 'find' SSH call.
        """
        # Use $HOME expansion — must NOT be quoted so remote shell expands it
        rpath = remote_path.replace("~/", "$HOME/").replace("~", "$HOME")
        r = ssh_obj.run(f'find {rpath} -type f -printf "%s\\t%T@\\t%P\\n" 2>/dev/null')
        result: Dict[str, Tuple[int, float]] = {}
        for line in r.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                try:
                    result[parts[2]] = (int(parts[0]), float(parts[1]))
                except (ValueError, IndexError):
                    continue
        return result

    def compute_delta(
        self, sync_dir_index: int, include_deletes: bool = False
    ) -> FileDelta:
        """
        Compute file delta by comparing old vs new server via find.
        Two SSH calls total — much faster than rsync --dry-run over VPN.
        """
        sd = self.cfg.sync_dirs[sync_dir_index]
        old_path = f"{self.old._cfg.wp_root}/{sd.source}"
        new_path = f"{self.new._cfg.wp_root}/{sd.dest}"

        old_files = self._list_remote_files(self.old, old_path)
        new_files = self._list_remote_files(self.new, new_path)

        new_list: List[FileChange] = []
        modified_list: List[FileChange] = []
        deleted_list: List[FileChange] = []

        for relpath, (old_size, old_mtime) in old_files.items():
            if relpath not in new_files:
                new_list.append(FileChange(relpath, "new", old_size))
            else:
                new_size, new_mtime = new_files[relpath]
                # Differ if size differs OR mtime differs by more than 1 second
                if old_size != new_size or abs(old_mtime - new_mtime) > 1.0:
                    modified_list.append(FileChange(relpath, "modified", old_size))

        if include_deletes:
            for relpath in new_files:
                if relpath not in old_files:
                    deleted_list.append(FileChange(relpath, "deleted", 0))

        return FileDelta(
            sync_dir_source=sd.source,
            sync_dir_dest=sd.dest,
            new_files=new_list,
            modified_files=modified_list,
            deleted_files=deleted_list,
        )

    def compute_all_deltas(self, include_deletes: bool = False) -> List[FileDelta]:
        deltas = []
        for i in range(len(self.cfg.sync_dirs)):
            delta = self.compute_delta(i, include_deletes)
            deltas.append(delta)
        return deltas

    # -- Sync (two-hop via local machine) ------------------------------------

    def sync_files(
        self,
        sync_dir_index: int,
        job_id: int,
        dry_run: bool = True,
    ) -> Dict:
        """
        Execute file sync for one sync_dir using delta-based --files-from approach.
        Only transfers files that are actually new or modified (from compute_delta).
        Phase 1: rsync only the delta files from old server → local staging dir
        Phase 2: rsync those same files from local staging → new server
        """
        sd = self.cfg.sync_dirs[sync_dir_index]
        old_src = f"{self.old.ssh_host}:{self.old._cfg.wp_root}/{sd.source}/"
        new_dst = f"{self.new.ssh_host}:{self.new._cfg.wp_root}/{sd.dest}/"
        local_stage = str(Path(self.temp_dir) / f"stage_{sd.source.replace('/', '_')}")
        Path(local_stage).mkdir(parents=True, exist_ok=True)

        self.db.log(job_id, f"[Motor A] Sync dir: {sd.source} → {sd.dest}", "INFO")

        # Compute delta to get exact list of files to transfer
        self.db.log(job_id, "Počítám deltu (find na obou serverech)...", "INFO")
        delta = self.compute_delta(sync_dir_index, include_deletes=False)
        files_to_transfer = [f.relative_path for f in delta.new_files + delta.modified_files]

        if not files_to_transfer:
            self.db.log(job_id, "Žádné soubory k přenosu — vše synchronizováno.", "INFO")
            return {"dry_run": dry_run, "sync_dir": sd.source, "uploaded_new": 0, "uploaded_modified": 0}

        total = len(files_to_transfer)
        self.db.log(job_id, f"K přenosu: {len(delta.new_files)} nových + {len(delta.modified_files)} změněných = {total} souborů", "INFO")

        if dry_run:
            self.db.log(job_id, f"DRY-RUN: přeskakuji přenos {total} souborů", "INFO")
            return {
                "dry_run": True,
                "sync_dir": sd.source,
                "uploaded_new": len(delta.new_files),
                "uploaded_modified": len(delta.modified_files),
            }

        # Write file list for --files-from
        filelist_path = Path(self.temp_dir) / f"filelist_{sd.source.replace('/', '_')}.txt"
        filelist_path.write_text("\n".join(files_to_transfer), encoding="utf-8")

        ssh_old = _build_old_ssh_opts(self.old)
        ssh_new = _build_new_ssh_opts(self.new)

        # Phase 1: download only delta files from old server → local staging
        cmd1 = [
            "rsync", "-az", "--files-from", str(filelist_path),
            "--out-format=%i %l %n",
            "-e", ssh_old, old_src, local_stage + "/",
        ]
        self.db.log(job_id, f"Fáze 1: stahuji {total} souborů ze starého serveru → lokálně...", "INFO")
        rc1, out1, err1 = _run_rsync_streaming(cmd1, job_id, self.db, log_every=10, timeout=1800)
        if rc1 not in (0, 24):
            msg = f"Fáze 1 selhala rc={rc1}: {err1[:300]}"
            self.db.log(job_id, msg, "ERROR")
            return {"error": msg, "phase": 1}

        staged_count = out1.count("\n") + (1 if out1 else 0)
        self.db.log(job_id, f"Fáze 1 hotová: {staged_count} souborů staženo lokálně", "INFO")

        # Phase 2: upload staged files Mac → new server
        cmd2 = [
            "rsync", "-az", "--files-from", str(filelist_path),
            "--out-format=%i %l %n",
            "-e", ssh_new, local_stage + "/", new_dst,
        ]
        self.db.log(job_id, f"Fáze 2: nahrávám {total} souborů lokálně → nový server...", "INFO")
        rc2, out2, err2 = _run_rsync_streaming(cmd2, job_id, self.db, log_every=10, timeout=1800)
        if rc2 not in (0, 24):
            msg = f"Fáze 2 selhala rc={rc2}: {err2[:300]}"
            self.db.log(job_id, msg, "ERROR")
            return {"error": msg, "phase": 2}

        uploaded_count = out2.count("\n") + (1 if out2 else 0)
        self.db.log(job_id, f"Fáze 2 hotová: {uploaded_count} souborů nahráno na nový server", "INFO")

        return {
            "dry_run": False,
            "sync_dir": sd.source,
            "uploaded_new": len(delta.new_files),
            "uploaded_modified": len(delta.modified_files),
        }

    def sync_all_files(self, job_id: int, dry_run: bool = True) -> List[Dict]:
        results = []
        for i in range(len(self.cfg.sync_dirs)):
            r = self.sync_files(i, job_id, dry_run=dry_run)
            results.append(r)
        return results

    # -- Deletion (requires backup guard, second confirmation) ----------------

    def delete_mirror(
        self,
        sync_dir_index: int,
        job_id: int,
        backup_path: str,
    ) -> Dict:
        """
        Mirror deletion: remove files from new server that no longer exist on old.
        REQUIRES: backup_path must point to a valid, just-created DB backup.
        Returns summary dict.
        """
        if not backup_path or not Path(backup_path).exists():
            # Try remote path (backup is on new server)
            msg = "Mirror delete refused: no valid backup path provided"
            self.db.log(job_id, msg, "ERROR")
            return {"error": msg}

        sd = self.cfg.sync_dirs[sync_dir_index]
        local_stage = str(Path(self.temp_dir) / f"stage_{sd.source.replace('/', '_')}")
        new_dst = f"{self.new.ssh_host}:{self.new._cfg.wp_root}/{sd.dest}/"
        ssh_new = _build_new_ssh_opts(self.new)

        cmd = [
            "rsync", "-avz", "--delete",
            "--out-format=%i %l %n",
            "-e", ssh_new,
            local_stage + "/",
            new_dst,
        ]
        rc, out, err = _run_rsync(cmd, timeout=1800)
        if rc not in (0, 24):
            msg = f"Mirror delete failed rc={rc}: {err[:300]}"
            self.db.log(job_id, msg, "ERROR")
            return {"error": msg}

        _, _, del_f = _parse_rsync_dry_run(out)
        self.db.log(job_id, f"Mirror delete: {len(del_f)} files removed from new server", "INFO")
        return {"deleted_files": len(del_f), "sync_dir": sd.source}
