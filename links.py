from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from wikilinks import extract_wikilink_targets


@dataclass
class LinkIndex:
    """
    Bidirectional link index for notes.

    outgoing[src] = {dst1, dst2, ...}
    incoming[dst] = {src1, src2, ...}

    `dst` may be virtual (note does not exist yet).
    """

    # note_id-based graph
    outgoing: dict[str, set[str]] = field(default_factory=dict)  # src_id -> {dst_id}
    incoming: dict[str, set[str]] = field(default_factory=dict)  # dst_id -> {src_id}

    # ───────────────────────── public API ─────────────────────────

    def clear(self) -> None:
        self.outgoing.clear()
        self.incoming.clear()

    def rebuild_from_vault(
        self,
        vault_dir: Path,
        *,
        resolve_title_to_id: Callable[[str], Optional[str]],
        path_to_id: Callable[[Path], Optional[str]],
    ) -> None:
        """
        Full rebuild from disk.
        Expensive, but safe.
        """
        self.clear()

        for path in vault_dir.rglob("*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                # corrupted / unreadable note → skip
                continue
            src_id = path_to_id(path)
            if not src_id:
                continue
            self.update_note(src_id, text, resolve_title_to_id=resolve_title_to_id)

    def update_note(
        self,
        src_id: str,
        markdown_text: str,
        *,
        resolve_title_to_id: Callable[[str], Optional[str]],
    ) -> bool:
        """
        Incrementally update index for a single note.

        Returns True if outgoing links actually changed.
        """
        if not src_id:
            return False

        # wikilinks are titles; resolve to note_id (skip unresolved targets)
        new_targets_title = extract_wikilink_targets(markdown_text)
        new_targets: set[str] = set()
        for t in new_targets_title:
            dst_id = resolve_title_to_id(t)
            if dst_id and dst_id != src_id:
                new_targets.add(dst_id)

        old_targets = self.outgoing.get(src_id, set())

        if new_targets == old_targets:
            return False

        # 1. Remove obsolete incoming links
        for removed in old_targets - new_targets:
            incoming_set = self.incoming.get(removed)
            if incoming_set:
                incoming_set.discard(src_id)
                if not incoming_set:
                    self.incoming.pop(removed, None)

        # 2. Add new incoming links
        for added in new_targets - old_targets:
            self.incoming.setdefault(added, set()).add(src_id)

        # 3. Update outgoing
        if new_targets:
            self.outgoing[src_id] = set(new_targets)
        else:
            self.outgoing.pop(src_id, None)

        return True

    def backlinks_for(self, target_id: str) -> list[str]:
        """
        Return sorted list of notes linking to target.
        """
        return sorted(self.incoming.get(target_id, set()), key=str.lower)