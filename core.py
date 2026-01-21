
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class LinkIndex:
    """
    Индекс ссылок:
        outgoing[src] = {dst1, dst2, ...}
        incoming[dst] = {src1, src2, ...}

    dst может быть "виртуальным" (заметка ещё не существует как файл).
    """
    outgoing: dict[str, set[str]] = field(default_factory=dict)
    incoming: dict[str, set[str]] = field(default_factory=dict)

    def clear(self) -> None:
        self.outgoing.clear()
        self.incoming.clear()

    def rebuild_from_vault(self, vault_dir: Path) -> None:
        self.clear()
        for p in vault_dir.glob("*.md"):
            src = p.stem
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            self.update_note(src, text)

    def update_note(self, src: str, markdown_text: str) -> bool:
        """
        Инкрементально обновляет индекс для одной заметки src.
        Возвращает True, если набор исходящих ссылок изменился (может требоваться перестройка графа/беклинков).
        """
        src = safe_filename(src)
        if not src:
            return False

        new_targets = extract_wikilink_targets(markdown_text)
        # self-links не держим
        new_targets.discard(src)

        old_targets = set(self.outgoing.get(src, ()))
        if old_targets == new_targets:
            return False
        # убрать старые обратные связи
        for dst in old_targets - new_targets:
            inc = self.incoming.get(dst)
            if inc:
                inc.discard(src)
                if not inc:
                    self.incoming.pop(dst, None)

        # добавить новые обратные связи
        for dst in new_targets - old_targets:
            self.incoming.setdefault(dst, set()).add(src)

        # обновить outgoing
        if new_targets:
            self.outgoing[src] = set(new_targets)
        else:
            self.outgoing.pop(src, None)
        return True

    def backlinks_for(self, target: str) -> list[str]:
        target = safe_filename(target)
        refs = sorted(self.incoming.get(target, set()), key=str.lower)
        return refs