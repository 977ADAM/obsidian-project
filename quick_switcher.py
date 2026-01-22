
from PySide6.QtWidgets import QDialog, QLineEdit, QListWidget, QVBoxLayout

class QuickSwitcherDialog(QDialog):
    def __init__(self, parent, get_titles, on_open):
        super().__init__(parent)
        self.setWindowTitle("Quick Switcher")
        self.setModal(True)
        self.resize(520, 420)

        self.get_titles = get_titles   # функция -> list[str]
        self.on_open = on_open         # функция(title)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Введите название… (Enter — открыть/создать)")
        self.listw = QListWidget()

        layout = QVBoxLayout(self)
        layout.addWidget(self.input)
        layout.addWidget(self.listw)

        self._all = []
        self._reload()

        self.input.textChanged.connect(self._filter)
        self.input.returnPressed.connect(self._open_current)
        self.listw.itemActivated.connect(lambda it: self._open_title(it.text()))

        # UX: сразу фокус в поле ввода
        self.input.setFocus()

    def _reload(self):
        self._all = sorted(self.get_titles(), key=str.lower)
        self._filter(self.input.text())

    def _filter(self, text: str):
        q = (text or "").strip().lower()
        self.listw.clear()

        if not q:
            # когда пусто — показываем первые N (как "recent" упрощенно)
            for t in self._all[:40]:
                self.listw.addItem(t)
            if self.listw.count():
                self.listw.setCurrentRow(0)
            return

        # простое fuzzy-ish: сначала contains, потом startswith, потом остальные
        contains = [t for t in self._all if q in t.lower()]
        starts = [t for t in contains if t.lower().startswith(q)]
        rest = [t for t in contains if t not in starts]
        ranked = starts + rest

        for t in ranked[:80]:
            self.listw.addItem(t)

        if self.listw.count():
            self.listw.setCurrentRow(0)

    def _open_current(self):
        text = self.input.text().strip()
        if not text:
            return

        cur = self.listw.currentItem()
        if cur:
            self._open_title(cur.text())
            return

        # если нет совпадений — создаём по введенному
        self._open_title(text)

    def _open_title(self, title: str):
        self.on_open(title)
        self.accept()