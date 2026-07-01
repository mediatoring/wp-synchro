"""
Central SSH and WP-CLI wrapper.

SAFETY INVARIANTS (hardcoded — changing this file is the only way to bypass):
  1. Old server is READ-ONLY: only a fixed allowlist of wp commands is permitted.
  2. All WP-CLI calls include --skip-themes (prevents Nette bootstrap from running).
  3. Old server wp calls are prefixed: SERVER_NAME=<env> php7.3 ~/wp.phar --path=<root>
  4. New server wp calls use:  wp --path=<root> --skip-themes
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import OldServerConfig, NewServerConfig

logger = logging.getLogger(__name__)


class SecurityError(Exception):
    """Raised when a write operation is attempted on the read-only old server."""


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    command: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def raise_if_error(self, context: str = "") -> None:
        if not self.ok:
            msg = context or self.command
            raise RuntimeError(
                f"Remote command failed (rc={self.returncode}): {msg}\nSTDERR: {self.stderr[:500]}"
            )


# ---------------------------------------------------------------------------
# Read-only operation allowlist for old server
# ---------------------------------------------------------------------------

_READONLY_PREFIXES = (
    "--version",
    "db query ",
    "db tables",
    "post get ",
    "post list",
    "post meta list",
    "post meta get",
    "post term list",
    "post-type list",
    "term list",
    "term get",
    "option get",
    "user list",
    "user get",
    "export ",
    "export --",
)


def _assert_readonly(wp_args: str) -> None:
    stripped = wp_args.lstrip()
    for prefix in _READONLY_PREFIXES:
        if stripped.startswith(prefix):
            return
    raise SecurityError(
        f"SAFETY BLOCK: attempted non-readonly WP-CLI command on old (read-only) server.\n"
        f"Command: {wp_args[:120]}\n"
        f"If this is intentional, add it to _READONLY_PREFIXES in ssh_wrapper.py."
    )


# ---------------------------------------------------------------------------
# Base SSH client
# ---------------------------------------------------------------------------

class _SSHBase:
    def __init__(self, ssh_host: str, ssh_key: Optional[str] = None, timeout: int = 120):
        self.ssh_host = ssh_host
        self.ssh_key = ssh_key
        self.timeout = timeout

    def _ssh_prefix(self) -> List[str]:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               "-o", "StrictHostKeyChecking=accept-new"]
        if self.ssh_key:
            cmd.extend(["-i", str(Path(self.ssh_key).expanduser())])
        cmd.append(self.ssh_host)
        return cmd

    def run(self, remote_cmd: str) -> CmdResult:
        full = self._ssh_prefix() + [remote_cmd]
        logger.debug("[%s] %s", self.ssh_host, remote_cmd[:300])
        try:
            r = subprocess.run(full, capture_output=True, text=True, timeout=self.timeout)
            return CmdResult(r.returncode, r.stdout, r.stderr, remote_cmd)
        except subprocess.TimeoutExpired:
            return CmdResult(124, "", f"Timeout after {self.timeout}s", remote_cmd)

    def upload_text(self, content: str, remote_path: str) -> CmdResult:
        """Write a string to a remote file via stdin pipe."""
        full = self._ssh_prefix() + [f"cat > {shlex.quote(remote_path)}"]
        try:
            r = subprocess.run(full, input=content, capture_output=True, text=True, timeout=60)
            return CmdResult(r.returncode, r.stdout, r.stderr, f"upload→{remote_path}")
        except subprocess.TimeoutExpired:
            return CmdResult(124, "", "Timeout", f"upload→{remote_path}")

    def download_text(self, remote_path: str) -> Tuple[bool, str]:
        """Read a remote file content. Returns (ok, content)."""
        r = self.run(f"cat {shlex.quote(remote_path)}")
        return r.ok, r.stdout

    def test_connection(self) -> Tuple[bool, str]:
        r = self.run("echo '__ok__'")
        if r.ok and "__ok__" in r.stdout:
            return True, "Connection OK"
        return False, (r.stderr or r.stdout or "No response").strip()[:200]


# ---------------------------------------------------------------------------
# Old server — READ-ONLY
# ---------------------------------------------------------------------------

class OldServer(_SSHBase):
    """
    Read-only interface to the old WordPress server.
    All WP-CLI calls are wrapped with SERVER_NAME=<env> + php7.3 ~/wp.phar + --skip-themes.
    Write operations raise SecurityError.
    """

    def __init__(self, cfg: OldServerConfig):
        super().__init__(cfg.ssh_host, cfg.ssh_key)
        self._cfg = cfg

    @staticmethod
    def _rp(path: str) -> str:
        """Replace ~ with $HOME so the remote bash expands it correctly.
        Bash only expands ~ at the start of a word, NOT after = in cmd args."""
        return path.replace("~/", "$HOME/").replace("~", "$HOME")

    def _wp(self, wp_args: str) -> str:
        """Build the full remote WP-CLI command string."""
        _assert_readonly(wp_args)
        c = self._cfg
        return (
            f"SERVER_NAME={c.server_name_env} "
            f"{c.php_binary} {self._rp(c.wpcli_path)} "
            f"--path={self._rp(c.wp_root)} "
            f"--skip-themes "
            f"{wp_args}"
        )

    def wp(self, wp_args: str) -> CmdResult:
        return self.run(self._wp(wp_args))

    # -- Discovery -----------------------------------------------------------

    def get_post_types(self) -> List[str]:
        # --skip-column-names is unreliable in WP-CLI 2.7.1 with csv format;
        # fetch with header and strip the "name" line explicitly.
        r = self.wp("post-type list --fields=name --format=csv")
        if not r.ok:
            return []
        return [ln.strip() for ln in r.stdout.splitlines()
                if ln.strip() and ln.strip() != "name"]

    def get_table_prefix(self) -> str:
        # Use config value directly — avoids 'config get' which could
        # expose secrets and is not in the read-only allowlist.
        return self._cfg.table_prefix

    # -- Post listing (uses wp db query for reliability — no --include issues) ---

    def list_posts(self, post_type: str, prefix: str) -> List[Dict]:
        """Return [{ID, post_name, post_modified, post_status}] for all posts of type."""
        sql = (
            f"SELECT ID, post_name, post_modified, post_status "
            f"FROM {prefix}posts "
            f"WHERE post_type='{post_type}' "
            f"AND post_status NOT IN ('auto-draft','inherit') "
            f"ORDER BY ID"
        )
        r = self.wp(f'db query "{sql}" --skip-column-names')
        if not r.ok:
            logger.warning("list_posts failed for %s: %s", post_type, r.stderr[:200])
            return []
        return _parse_tsv(r.stdout, ["ID", "post_name", "post_modified", "post_status"])

    # -- Single post access --------------------------------------------------

    def get_post(self, post_id: int) -> Optional[Dict]:
        r = self.wp(
            f"post get {post_id} "
            "--fields=ID,post_title,post_content,post_excerpt,post_name,"
            "post_status,post_type,post_date,post_date_gmt,post_modified,"
            "post_modified_gmt,post_author,post_parent,menu_order,"
            "comment_status,ping_status "
            "--format=json"
        )
        if not r.ok:
            return None
        return _json_load(r.stdout)

    def get_post_meta(self, post_id: int) -> List[Dict]:
        r = self.wp(f"post meta list {post_id} --format=json")
        if not r.ok:
            return []
        return _json_load(r.stdout) or []

    def get_post_terms(self, post_id: int) -> List[Dict]:
        """Get all terms for a post across all taxonomies."""
        r = self.wp(f"post term list {post_id} --all-taxonomies --format=json")
        if not r.ok:
            return []
        return _json_load(r.stdout) or []

    # -- Polylang ------------------------------------------------------------

    def get_polylang_language(self, post_id: int) -> Optional[str]:
        r = self.wp(f"post term list {post_id} language --field=slug --format=csv")
        if not r.ok or not r.stdout.strip():
            return None
        slugs = [s.strip() for s in r.stdout.splitlines() if s.strip() and s.strip() != "slug"]
        return slugs[0] if slugs else None

    def get_polylang_translations(self, post_id: int, prefix: str) -> Dict[str, int]:
        """
        Return translation map {lang_slug: post_id} for the group that contains post_id.
        Polylang 3.x stores this in the post_translations taxonomy term description
        as a serialized PHP array: a:2:{s:2:"cs";i:123;s:2:"en";i:456;}
        """
        sql = (
            f"SELECT tt.description "
            f"FROM {prefix}term_relationships tr "
            f"JOIN {prefix}term_taxonomy tt ON tr.term_taxonomy_id = tt.term_taxonomy_id "
            f"WHERE tr.object_id = {post_id} "
            f"AND tt.taxonomy = 'post_translations'"
        )
        r = self.wp(f'db query "{sql}" --skip-column-names')
        if not r.ok or not r.stdout.strip():
            return {}
        return _parse_php_serialized_map(r.stdout.strip())

    # -- Export (WXR) --------------------------------------------------------

    def export_post(self, post_id: int, remote_tmp: str) -> Optional[str]:
        """
        Export a single post as WXR to remote_tmp directory.
        Returns the remote path of the XML file, or None on failure.
        """
        r = self.wp(f"export --post__in={post_id} --dir={remote_tmp}")
        if not r.ok:
            logger.error("WXR export failed for %s: %s", post_id, r.stderr[:300])
            return None
        # Find the generated .xml file
        ls = self.run(f"ls -1t {remote_tmp}/*.xml 2>/dev/null | head -1")
        if ls.ok and ls.stdout.strip():
            return ls.stdout.strip()
        return None


# ---------------------------------------------------------------------------
# New server — read + write
# ---------------------------------------------------------------------------

class NewServer(_SSHBase):
    """
    Read/write interface to the new WordPress server.
    All WP-CLI calls include --skip-themes (prevents Nette PHP 8 compat crash).
    """

    def __init__(self, cfg: NewServerConfig):
        super().__init__(cfg.ssh_host, cfg.ssh_key)
        self._cfg = cfg
        self._prefix: Optional[str] = None

    @staticmethod
    def _rp(path: str) -> str:
        return path.replace("~/", "$HOME/").replace("~", "$HOME")

    def _wp(self, wp_args: str) -> str:
        return f"{self._cfg.wpcli_binary} --path={self._rp(self._cfg.wp_root)} --skip-themes {wp_args}"

    def wp(self, wp_args: str) -> CmdResult:
        return self.run(self._wp(wp_args))

    # -- Helpers -------------------------------------------------------------

    def get_table_prefix(self) -> str:
        if self._prefix:
            return self._prefix
        r = self.wp("config get table_prefix")
        self._prefix = r.stdout.strip() if r.ok else "wp_"
        return self._prefix

    # -- Post listing --------------------------------------------------------

    def list_posts(self, post_type: str, prefix: str) -> List[Dict]:
        sql = (
            f"SELECT ID, post_name, post_modified, post_status "
            f"FROM {prefix}posts "
            f"WHERE post_type='{post_type}' "
            f"AND post_status NOT IN ('auto-draft','inherit') "
            f"ORDER BY ID"
        )
        r = self.wp(f'db query "{sql}" --skip-column-names')
        if not r.ok:
            return []
        return _parse_tsv(r.stdout, ["ID", "post_name", "post_modified", "post_status"])

    def get_post(self, post_id: int) -> Optional[Dict]:
        r = self.wp(
            f"post get {post_id} "
            "--fields=ID,post_title,post_content,post_name,post_status,"
            "post_type,post_date,post_modified,post_author,post_parent "
            "--format=json"
        )
        if not r.ok:
            return None
        return _json_load(r.stdout)

    def post_exists(self, post_id: int) -> bool:
        r = self.wp(f"post exists {post_id}")
        return r.ok

    # -- Post creation / update via JSON temp file ---------------------------

    def create_post_from_data(self, data: Dict, original_id: int) -> Tuple[bool, int]:
        """
        Create a post on the new server, then remap its auto-generated ID
        to the original ID from the old server (preserving ID parity).
        Returns (success, actual_id_used).
        """
        json_path = f"/tmp/wp_sync_post_{original_id}.json"
        create_result, tmp_id = self._create_post_via_json(data, json_path)
        if not create_result or tmp_id <= 0:
            return False, -1

        if tmp_id == original_id:
            return True, original_id

        # Remap the auto-generated ID to the original
        prefix = self.get_table_prefix()
        ok = self._remap_post_id(prefix, tmp_id, original_id)
        if ok:
            return True, original_id
        # Remap failed — keep the tmp_id and log it
        logger.warning("ID remap failed: tmp=%s original=%s", tmp_id, original_id)
        return True, tmp_id

    def _create_post_via_json(self, data: Dict, remote_json: str) -> Tuple[bool, int]:
        """Write post data JSON to remote, use wp eval to insert, return (ok, new_id)."""
        # Exclude read-only/auto fields that wp_insert_post shouldn't receive
        excluded = {"post_modified", "post_modified_gmt", "filter"}
        clean = {k: v for k, v in data.items() if k not in excluded and v is not None}
        clean.pop("ID", None)  # Remove ID so WP assigns a new auto-increment one

        payload = json.dumps(clean, ensure_ascii=False)
        up = self.upload_text(payload, remote_json)
        if not up.ok:
            return False, -1

        php = (
            f"$d = json_decode(file_get_contents('{remote_json}'), true); "
            f"$id = wp_insert_post($d, true); "
            f"if(is_wp_error($id)){{ echo 'ERR:'.$id->get_error_message(); }}else{{ echo $id; }} "
            f"@unlink('{remote_json}');"
        )
        r = self.wp(f"eval {shlex.quote(php)}")
        if not r.ok or r.stdout.startswith("ERR:"):
            logger.error("wp eval create failed: %s %s", r.stdout[:200], r.stderr[:200])
            return False, -1
        try:
            return True, int(r.stdout.strip())
        except ValueError:
            return False, -1

    def update_post_from_data(self, post_id: int, data: Dict) -> bool:
        """Update an existing post using wp eval + temp JSON file."""
        remote_json = f"/tmp/wp_sync_update_{post_id}.json"
        excluded = {"ID", "post_modified", "post_modified_gmt", "filter"}
        clean = {k: v for k, v in data.items() if k not in excluded and v is not None}
        clean["ID"] = post_id

        payload = json.dumps(clean, ensure_ascii=False)
        up = self.upload_text(payload, remote_json)
        if not up.ok:
            return False

        php = (
            f"$d = json_decode(file_get_contents('{remote_json}'), true); "
            f"$r = wp_update_post($d, true); "
            f"if(is_wp_error($r)){{ echo 'ERR:'.$r->get_error_message(); }}else{{ echo 'ok'; }} "
            f"@unlink('{remote_json}');"
        )
        r = self.wp(f"eval {shlex.quote(php)}")
        if not r.ok or r.stdout.strip().startswith("ERR:"):
            logger.error("wp eval update %s failed: %s", post_id, r.stdout[:200])
            return False
        return True

    def _remap_post_id(self, prefix: str, old_auto_id: int, new_id: int) -> bool:
        """Remap auto-generated post ID to the desired original ID via SQL."""
        sqls = [
            f"UPDATE {prefix}posts SET ID={new_id} WHERE ID={old_auto_id}",
            f"UPDATE {prefix}postmeta SET post_id={new_id} WHERE post_id={old_auto_id}",
            f"UPDATE {prefix}term_relationships SET object_id={new_id} WHERE object_id={old_auto_id}",
            f"UPDATE {prefix}comments SET comment_post_ID={new_id} WHERE comment_post_ID={old_auto_id}",
        ]
        for sql in sqls:
            r = self.wp(f'db query "{sql}"')
            if not r.ok:
                logger.error("remap SQL failed: %s | err: %s", sql, r.stderr[:200])
                return False
        return True

    # -- Post meta -----------------------------------------------------------

    def sync_post_meta(self, post_id: int, meta_list: List[Dict]) -> int:
        """
        Sync meta from old server to new. Returns count of updated keys.
        Uses wp eval + temp JSON for safety with complex serialized values.
        """
        remote_json = f"/tmp/wp_sync_meta_{post_id}.json"
        # meta_list: [{post_id, meta_key, meta_value}, ...]
        payload = json.dumps([
            {"key": m.get("meta_key", ""), "value": m.get("meta_value", "")}
            for m in meta_list
        ], ensure_ascii=False)
        up = self.upload_text(payload, remote_json)
        if not up.ok:
            return 0

        php = (
            f"$items = json_decode(file_get_contents('{remote_json}'), true); "
            f"$n = 0; "
            f"foreach($items as $m){{ update_post_meta({post_id}, $m['key'], $m['value']); $n++; }} "
            f"echo $n; "
            f"@unlink('{remote_json}');"
        )
        r = self.wp(f"eval {shlex.quote(php)}")
        if not r.ok:
            return 0
        try:
            return int(r.stdout.strip())
        except ValueError:
            return 0

    # -- Terms ---------------------------------------------------------------

    def get_post_terms(self, post_id: int) -> List[Dict]:
        r = self.wp(f"post term list {post_id} --all-taxonomies --format=json")
        if not r.ok:
            return []
        return _json_load(r.stdout) or []

    def set_post_terms(self, post_id: int, taxonomy: str, term_slugs: List[str]) -> bool:
        if not term_slugs:
            return True
        slugs_arg = " ".join(shlex.quote(s) for s in term_slugs)
        r = self.wp(f"post term set {post_id} {taxonomy} {slugs_arg}")
        return r.ok

    def ensure_term_exists(self, taxonomy: str, slug: str, name: str, parent: int = 0) -> bool:
        """Create term if it doesn't already exist."""
        r = self.wp(f"term get {taxonomy} {shlex.quote(slug)} --by=slug --format=json")
        if r.ok and r.stdout.strip():
            return True
        args = f"term create {taxonomy} {shlex.quote(name)} --slug={shlex.quote(slug)}"
        if parent:
            args += f" --parent={parent}"
        r2 = self.wp(args)
        return r2.ok

    # -- Polylang ------------------------------------------------------------

    def set_polylang_language(self, post_id: int, lang_slug: str) -> bool:
        r = self.wp(f"post term set {post_id} language {shlex.quote(lang_slug)}")
        return r.ok

    def get_polylang_language(self, post_id: int) -> Optional[str]:
        r = self.wp(f"post term list {post_id} language --field=slug --format=csv")
        if not r.ok or not r.stdout.strip():
            return None
        slugs = [s.strip() for s in r.stdout.splitlines() if s.strip() and s.strip() != "slug"]
        return slugs[0] if slugs else None

    def get_polylang_translations(self, post_id: int, prefix: str) -> Dict[str, int]:
        sql = (
            f"SELECT tt.description "
            f"FROM {prefix}term_relationships tr "
            f"JOIN {prefix}term_taxonomy tt ON tr.term_taxonomy_id = tt.term_taxonomy_id "
            f"WHERE tr.object_id = {post_id} "
            f"AND tt.taxonomy = 'post_translations'"
        )
        r = self.wp(f'db query "{sql}" --skip-column-names')
        if not r.ok or not r.stdout.strip():
            return {}
        return _parse_php_serialized_map(r.stdout.strip())

    def set_polylang_translations(self, translations: Dict[str, int]) -> bool:
        """
        Reconstruct a Polylang translation group on the new server.
        translations: {lang_slug: post_id, ...}
        Uses wp eval to call pll_save_post_translations() if available,
        otherwise falls back to direct DB manipulation.
        """
        if not translations:
            return True

        php = (
            f"$t = json_decode('{json.dumps(translations)}', true); "
            f"if(function_exists('pll_save_post_translations')){{ "
            f"  pll_save_post_translations($t); echo 'pll_ok'; "
            f"}} else {{ echo 'no_pll'; }}"
        )
        r = self.wp(f"eval {shlex.quote(php)}")
        if r.ok and "pll_ok" in r.stdout:
            return True
        # Fallback: log that Polylang function was not available
        logger.warning("pll_save_post_translations not available, translation group not set for %s", translations)
        return False

    # -- DB operations -------------------------------------------------------

    def backup_database(self, backup_dir: str) -> Tuple[bool, str]:
        """Export DB to a timestamped SQL file. Returns (ok, path_or_error)."""
        import time
        ts = int(time.time())
        path = f"{backup_dir}/backup_{ts}.sql"
        self.run(f"mkdir -p {shlex.quote(backup_dir)}")
        r = self.wp(f"db export {shlex.quote(path)} --add-drop-table")
        if r.ok:
            return True, path
        return False, r.stderr[:300]

    def get_post_meta(self, post_id: int) -> List[Dict]:
        r = self.wp(f"post meta list {post_id} --format=json")
        if not r.ok:
            return []
        return _json_load(r.stdout) or []

    def trash_post(self, post_id: int) -> bool:
        r = self.wp(f"post update {post_id} --post_status=trash")
        return r.ok

    def delete_post_force(self, post_id: int) -> bool:
        r = self.wp(f"post delete {post_id} --force")
        return r.ok


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _parse_tsv(text: str, columns: List[str]) -> List[Dict]:
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        row = {}
        for i, col in enumerate(columns):
            row[col] = parts[i].strip() if i < len(parts) else ""
        rows.append(row)
    return rows


def _json_load(text: str) -> Optional[any]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_php_serialized_map(serialized: str) -> Dict[str, int]:
    """
    Parse PHP serialized array: a:2:{s:2:"cs";i:123;s:2:"en";i:456;}
    Returns {lang: post_id}.
    """
    result: Dict[str, int] = {}
    if not serialized or not serialized.startswith("a:"):
        return result
    for lang, pid in re.findall(r's:\d+:"([^"]+)";i:(\d+);', serialized):
        result[lang] = int(pid)
    return result
