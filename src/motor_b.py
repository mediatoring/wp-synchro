"""
Motor B — Database content synchronization engine.

Flow:
  1. list_posts() on old and new for each post type → delta.
  2. For each NEW post: create on new (preserving ID), transfer meta + terms.
  3. For each CHANGED post: update on new, re-sync meta + terms.
  4. Polylang: apply language + translation group after each post.
  5. Deletion (mirror mode): separate second confirmation + DB backup guard.

Uses WP-CLI exclusively — never touches DB schema directly (to handle WP 5→7 diff).
The only SQL used is read-only SELECTs for listing and INSERT ID remapping.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import SyncConfig
from .ssh_wrapper import OldServer, NewServer
from .polylang import PolylangEngine, PolylangInfo
from .state_db import StateDB

logger = logging.getLogger(__name__)


@dataclass
class PostDiff:
    post_id: int
    change_type: str          # 'new' | 'modified' | 'deleted_from_old'
    post_type: str
    post_name: str
    post_status: str
    old_modified: str = ""
    new_modified: str = ""
    language: Optional[str] = None


@dataclass
class ContentDelta:
    post_type: str
    new_posts: List[PostDiff] = field(default_factory=list)
    modified_posts: List[PostDiff] = field(default_factory=list)
    deleted_posts: List[PostDiff] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return len(self.new_posts) + len(self.modified_posts)


# Meta keys that should NOT be transferred (internal WP / Polylang / caching keys)
_META_SKIP_PREFIXES = ("_edit_lock", "_edit_last", "_pll_", "total_cache", "w3tc")
_META_SKIP_EXACT = {"_wp_old_slug"}


def _should_skip_meta(key: str) -> bool:
    if key in _META_SKIP_EXACT:
        return True
    return any(key.startswith(p) for p in _META_SKIP_PREFIXES)


# Taxonomies managed by Polylang — skip from generic term sync
_POLYLANG_TAXONOMIES = {"language", "post_translations", "term_language", "term_translations"}


class MotorB:
    def __init__(
        self,
        cfg: SyncConfig,
        old: OldServer,
        new_: NewServer,
        db: StateDB,
        polylang: PolylangEngine,
    ):
        self.cfg = cfg
        self.old = old
        self.new = new_
        self.db = db
        self.polylang = polylang
        self._old_prefix: Optional[str] = None
        self._new_prefix: Optional[str] = None

    def _op(self) -> str:
        if not self._old_prefix:
            self._old_prefix = self.old.get_table_prefix()
        return self._old_prefix

    def _np(self) -> str:
        if not self._new_prefix:
            self._new_prefix = self.new.get_table_prefix()
        return self._new_prefix

    # -- Post type resolution ------------------------------------------------

    def get_active_post_types(self) -> List[str]:
        """
        All post types on old server minus the excluded list, plus any
        extra_post_types from config (theme-registered CPTs hidden by --skip-themes).
        """
        all_types = self.old.get_post_types()
        excluded = set(self.cfg.excluded_post_types)
        discovered = [t for t in all_types if t not in excluded]
        # Merge extra CPTs that WP-CLI post-type list misses due to --skip-themes
        for pt in self.cfg.extra_post_types:
            if pt not in excluded and pt not in discovered:
                discovered.append(pt)
        return discovered

    # -- Delta detection -----------------------------------------------------

    def compute_delta(self, post_type: str) -> ContentDelta:
        """Compare old vs new for one post type. Returns ContentDelta."""
        old_posts = {
            int(p["ID"]): p
            for p in self.old.list_posts(post_type, self._op())
        }
        new_posts = {
            int(p["ID"]): p
            for p in self.new.list_posts(post_type, self._np())
        }

        delta = ContentDelta(post_type=post_type)

        for pid, op in old_posts.items():
            np = new_posts.get(pid)
            if np is None:
                # Skip posts that are already trashed on old server —
                # no point creating a new trashed post on the target.
                if op.get("post_status") == "trash":
                    continue
                delta.new_posts.append(PostDiff(
                    post_id=pid,
                    change_type="new",
                    post_type=post_type,
                    post_name=op.get("post_name", ""),
                    post_status=op.get("post_status", ""),
                    old_modified=op.get("post_modified", ""),
                ))
            elif op.get("post_modified", "") > np.get("post_modified", ""):
                delta.modified_posts.append(PostDiff(
                    post_id=pid,
                    change_type="modified",
                    post_type=post_type,
                    post_name=op.get("post_name", ""),
                    post_status=op.get("post_status", ""),
                    old_modified=op.get("post_modified", ""),
                    new_modified=np.get("post_modified", ""),
                ))

        for pid, np in new_posts.items():
            if pid not in old_posts:
                delta.deleted_posts.append(PostDiff(
                    post_id=pid,
                    change_type="deleted_from_old",
                    post_type=post_type,
                    post_name=np.get("post_name", ""),
                    post_status=np.get("post_status", ""),
                    new_modified=np.get("post_modified", ""),
                ))

        return delta

    def compute_all_deltas(self, post_types: Optional[List[str]] = None) -> List[ContentDelta]:
        types = post_types or self.get_active_post_types()
        return [self.compute_delta(pt) for pt in types]

    # -- Language annotation on delta ----------------------------------------

    def annotate_delta_languages(self, delta: ContentDelta) -> None:
        """Fill in .language for each PostDiff (optional, can be slow for large deltas)."""
        posts = delta.new_posts + delta.modified_posts
        for diff in posts:
            diff.language = self.old.get_polylang_language(diff.post_id)

    # -- Single post sync ----------------------------------------------------

    def sync_post(self, post_id: int, job_id: int) -> Tuple[bool, str]:
        """
        Create or update a single post on the new server.
        Returns (success, message).
        """
        # Is it new or an update?
        post_exists_on_new = self.new.post_exists(post_id)

        # Fetch full post data from old
        post_data = self.old.get_post(post_id)
        if not post_data:
            msg = f"Could not fetch post {post_id} from old server"
            self.db.log(job_id, msg, "ERROR")
            return False, msg

        # Fetch Polylang info
        pll_info = self.polylang.get_info(post_id)

        if post_exists_on_new:
            ok = self.new.update_post_from_data(post_id, post_data)
            action = "updated"
        else:
            ok, actual_id = self.new.create_post_from_data(post_data, post_id)
            action = f"created (new_id={actual_id})"
            if ok:
                self.db.upsert_id_map(post_id, actual_id, post_data.get("post_type", ""))

        if not ok:
            msg = f"Post {post_id}: {action} — FAILED"
            self.db.log(job_id, msg, "ERROR")
            return False, msg

        # Sync postmeta
        meta_list = self.old.get_post_meta(post_id)
        filtered_meta = [m for m in meta_list if not _should_skip_meta(m.get("meta_key", ""))]
        synced_meta = self.new.sync_post_meta(post_id, filtered_meta)

        # Sync terms (non-Polylang taxonomies)
        old_terms = self.old.get_post_terms(post_id)
        terms_by_tax: Dict[str, List[str]] = {}
        for t in old_terms:
            tax = t.get("taxonomy", "")
            if tax in _POLYLANG_TAXONOMIES:
                continue
            terms_by_tax.setdefault(tax, []).append(t.get("slug", ""))
        for tax, slugs in terms_by_tax.items():
            self.new.set_post_terms(post_id, tax, slugs)

        # Apply Polylang
        id_map = {row["old_id"]: row["new_id"] for row in self.db.get_all_id_maps()}
        id_map[post_id] = post_id  # self-mapping
        pll_ok, pll_note = self.polylang.apply(post_id, pll_info, id_map)

        msg = (
            f"Post {post_id} ({post_data.get('post_type')}/{post_data.get('post_name')}): "
            f"{action}, meta={synced_meta}, polylang={pll_note}"
        )
        self.db.log(job_id, msg, "INFO")
        return True, msg

    # -- Batch sync ----------------------------------------------------------

    def sync_batch(
        self,
        diffs: List[PostDiff],
        job_id: int,
        dry_run: bool = True,
    ) -> Dict:
        """
        Sync a list of PostDiff entries. dry_run=True only logs what would happen.
        Returns summary counts.
        """
        results = {"ok": 0, "error": 0, "skipped": 0, "dry_run": dry_run}

        for diff in diffs:
            if dry_run:
                self.db.log(
                    job_id,
                    f"[DRY-RUN] Would sync post {diff.post_id} "
                    f"({diff.post_type}/{diff.post_name}) [{diff.change_type}]",
                )
                results["skipped"] += 1
                continue

            ok, msg = self.sync_post(diff.post_id, job_id)
            if ok:
                results["ok"] += 1
            else:
                results["error"] += 1

        return results

    def sync_delta(
        self,
        delta: ContentDelta,
        job_id: int,
        dry_run: bool = True,
        include_new: bool = True,
        include_modified: bool = True,
    ) -> Dict:
        """Sync new and/or modified posts from a ContentDelta."""
        diffs: List[PostDiff] = []
        if include_new:
            diffs.extend(delta.new_posts)
        if include_modified:
            diffs.extend(delta.modified_posts)

        # Respect batch limit
        if len(diffs) > self.cfg.batch_limit:
            self.db.log(
                job_id,
                f"Batch limit {self.cfg.batch_limit} applied, "
                f"{len(diffs) - self.cfg.batch_limit} posts deferred",
                "WARNING",
            )
            diffs = diffs[: self.cfg.batch_limit]

        return self.sync_batch(diffs, job_id, dry_run=dry_run)

    # -- Deletion (mirror mode, requires backup) --------------------------------

    def delete_post_on_new(self, post_id: int, job_id: int, backup_confirmed: bool) -> bool:
        """
        Trash a post on the new server that no longer exists on old.
        Requires backup_confirmed=True (caller must have created and verified a DB backup).
        """
        if not backup_confirmed:
            self.db.log(
                job_id,
                f"Delete of post {post_id} refused: no confirmed backup",
                "ERROR",
            )
            return False

        ok = self.new.trash_post(post_id)
        status = "trashed" if ok else "FAILED"
        self.db.log(job_id, f"Post {post_id} delete: {status}", "INFO" if ok else "ERROR")
        return ok

    # -- Polylang verification -----------------------------------------------

    def verify_polylang(self, post_ids: List[int], job_id: int) -> Dict:
        """
        Verify Polylang state for given post IDs after sync.
        Returns summary.
        """
        id_map = {row["old_id"]: row["new_id"] for row in self.db.get_all_id_maps()}
        results, ok, fail = self.polylang.verify_bulk(post_ids, id_map)

        self.db.log(
            job_id,
            f"Polylang verify: {ok} OK, {fail} mismatches out of {len(post_ids)} posts",
            "INFO" if fail == 0 else "WARNING",
        )

        mismatches = [
            {
                "post_id": r.post_id,
                "expected_lang": r.expected_lang,
                "actual_lang": r.actual_lang,
                "expected_translations": r.expected_translations,
                "actual_translations": r.actual_translations,
            }
            for r in results
            if not r.ok
        ]
        return {
            "total": len(post_ids),
            "ok": ok,
            "fail": fail,
            "mismatches": mismatches,
        }
