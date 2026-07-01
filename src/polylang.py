"""
Polylang 3.x language and translation-group handling.

Polylang stores:
  - Language assignment: term in taxonomy 'language' on each post
  - Translation groups: term in taxonomy 'post_translations' whose
    term_taxonomy.description is a PHP-serialized map {lang_slug: post_id}

After syncing a post we must:
  1. Set its language term on the new server.
  2. Rebuild the translation group so that cs↔en pairing is preserved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .ssh_wrapper import OldServer, NewServer

logger = logging.getLogger(__name__)


@dataclass
class PolylangInfo:
    post_id: int
    language: Optional[str]               # e.g. 'cs' | 'en'
    translation_group: Dict[str, int]     # {lang: post_id} for the whole group


@dataclass
class VerifyResult:
    post_id: int
    ok: bool
    expected_lang: Optional[str]
    actual_lang: Optional[str]
    expected_translations: Dict[str, int]
    actual_translations: Dict[str, int]


class PolylangEngine:
    def __init__(self, old: OldServer, new_: NewServer):
        self.old = old
        self.new = new_
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

    # -- Reading from old server ---------------------------------------------

    def get_info(self, post_id: int) -> PolylangInfo:
        lang = self.old.get_polylang_language(post_id)
        group = self.old.get_polylang_translations(post_id, self._op())
        return PolylangInfo(post_id=post_id, language=lang, translation_group=group)

    def get_info_bulk(self, post_ids: List[int]) -> List[PolylangInfo]:
        results = []
        for pid in post_ids:
            results.append(self.get_info(pid))
        return results

    # -- Applying to new server ----------------------------------------------

    def apply(self, post_id: int, info: PolylangInfo, id_map: Dict[int, int]) -> Tuple[bool, str]:
        """
        Apply language and translation group to post_id on the new server.
        id_map: {old_id → new_id} — used to translate translation group IDs.

        Returns (success, note).
        """
        success = True
        notes = []

        # 1. Set language
        if info.language:
            if not self.new.set_polylang_language(post_id, info.language):
                logger.warning("Failed to set language %s for post %s", info.language, post_id)
                success = False
                notes.append(f"language set failed ({info.language})")
            else:
                notes.append(f"language={info.language}")

        # 2. Rebuild translation group using mapped IDs
        if info.translation_group:
            # Translate old IDs to new IDs via the map
            new_group: Dict[str, int] = {}
            for lang, old_id in info.translation_group.items():
                new_id = id_map.get(old_id, old_id)  # fall back to same ID if mapping unknown
                new_group[lang] = new_id

            if not self.new.set_polylang_translations(new_group):
                logger.warning("Failed to set translation group for post %s: %s", post_id, new_group)
                success = False
                notes.append("translation group set failed")
            else:
                notes.append(f"translations={new_group}")

        return success, "; ".join(notes) if notes else "no polylang info"

    # -- Verification --------------------------------------------------------

    def verify(self, post_id: int, old_info: PolylangInfo, id_map: Dict[int, int]) -> VerifyResult:
        """
        Compare old server's Polylang info against what's actually on the new server.
        """
        actual_lang = self.new.get_polylang_language(post_id)
        actual_group = self.new.get_polylang_translations(post_id, self._np())

        # Translate expected group to new IDs
        expected_group: Dict[str, int] = {}
        for lang, old_id in old_info.translation_group.items():
            expected_group[lang] = id_map.get(old_id, old_id)

        lang_ok = old_info.language == actual_lang
        group_ok = expected_group == actual_group

        return VerifyResult(
            post_id=post_id,
            ok=lang_ok and group_ok,
            expected_lang=old_info.language,
            actual_lang=actual_lang,
            expected_translations=expected_group,
            actual_translations=actual_group,
        )

    def verify_bulk(
        self, post_ids: List[int], id_map: Dict[int, int]
    ) -> Tuple[List[VerifyResult], int, int]:
        """
        Verify Polylang state for a list of post IDs.
        Returns (results, ok_count, fail_count).
        """
        old_infos = {pid: self.get_info(pid) for pid in post_ids}
        results = []
        ok = 0
        fail = 0
        for pid in post_ids:
            info = old_infos[pid]
            result = self.verify(pid, info, id_map)
            results.append(result)
            if result.ok:
                ok += 1
            else:
                fail += 1
                logger.warning(
                    "Polylang mismatch on post %s: lang=%s≠%s group=%s≠%s",
                    pid,
                    result.expected_lang,
                    result.actual_lang,
                    result.expected_translations,
                    result.actual_translations,
                )
        return results, ok, fail
