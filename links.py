from dataclasses import dataclass, field
from pathlib import Path

from filenames import safe_filename
from wikilinks import extract_wikilink_targets


@dataclass
class LinkIndex:
    """
    Bidirectional link index for notes.

    outgoing[src] = {dst1, dst2, ...}
    incoming[dst] = {src1, src2, ...}

    `dst` may be virtual (note does not exist yet).
    """

    outgoing: dict[str, set[str]] = field(default_factory=dict)
    incoming: dict[str, set[str]] = field(default_factory=dict)

    # ───────────────────────── public API ─────────────────────────

    def clear(self) -> None:
        self.outgoing.clear()
        self.incoming.clear()

    def rebuild_from_vault(self, vault_dir: Path) -> None:
        """
        Full rebuild from disk.
        Expensive, but safe.
        """
        self.clear()

        for path in vault_dir.glob("*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                # corrupted / unreadable note → skip
                continue

            self.update_note(path.stem, text)

    def update_note(self, src: str, markdown_text: str) -> bool:
        """
        Incrementally update index for a single note.

        Returns True if outgoing links actually changed.
        """
        src = safe_filename(src)
        if not src:
            return False

        new_targets = extract_wikilink_targets(markdown_text)
        new_targets.discard(src)  # no self-links

        old_targets = self.outgoing.get(src, set())

        if new_targets == old_targets:
            return False

        # 1. Remove obsolete incoming links
        for removed in old_targets - new_targets:
            incoming_set = self.incoming.get(removed)
            if incoming_set:
                incoming_set.discard(src)
                if not incoming_set:
                    self.incoming.pop(removed, None)

        # 2. Add new incoming links
        for added in new_targets - old_targets:
            self.incoming.setdefault(added, set()).add(src)

        # 3. Update outgoing
        if new_targets:
            self.outgoing[src] = set(new_targets)
        else:
            self.outgoing.pop(src, None)

        return True

    def backlinks_for(self, target: str) -> list[str]:
        """
        Return sorted list of notes linking to target.
        """
        target = safe_filename(target)
        return sorted(self.incoming.get(target, set()), key=str.lower)