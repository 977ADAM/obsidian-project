from __future__ import annotations

from typing import Callable, Literal

from PySide6.QtWidgets import (
    QWidget,
    QMessageBox,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QTextEdit,
    QLineEdit,
    QPushButton,
)


def ask_vault_cancel_action(parent: QWidget) -> Literal["retry", "fallback", "exit"]:
    """
    Called when user cancels vault selection on first startup (no vault yet).
    Returns one of: 'retry', 'fallback', 'exit'.
    """
    msg = QMessageBox(parent)
    msg.setIcon(QMessageBox.Warning)
    msg.setWindowTitle("Хранилище не выбрано")
    msg.setText("Вы не выбрали папку для заметок.")
    msg.setInformativeText("Как поступить?")
    btn_retry = msg.addButton("Выбрать папку ещё раз", QMessageBox.AcceptRole)
    msg.addButton("Использовать резервную папку", QMessageBox.DestructiveRole)
    btn_exit = msg.addButton("Выйти", QMessageBox.RejectRole)
    msg.setDefaultButton(btn_retry)
    msg.exec()

    clicked = msg.clickedButton()
    if clicked == btn_retry:
        return "retry"
    if clicked == btn_exit:
        return "exit"
    return "fallback"


def build_rename_dialog(
    parent: QWidget,
    *,
    old_stem: str,
    on_rename: Callable[[str, str], bool],
) -> QDialog:
    """Small factory for the rename dialog to keep NotesApp slim."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("Переименовать заметку")
    dlg.setModal(True)
    dlg.resize(520, 140)

    layout = QVBoxLayout(dlg)

    info = QTextEdit()
    info.setReadOnly(True)
    info.setMaximumHeight(60)
    info.setPlainText(
        "Введите новое имя заметки.\n"
        "Будет переименован файл и обновлены ссылки вида [[...]] во всём хранилище."
    )
    layout.addWidget(info)

    inp = QLineEdit()
    inp.setPlaceholderText("Новое имя…")
    inp.setText(old_stem)
    inp.selectAll()
    layout.addWidget(inp)

    def do_accept() -> None:
        new_title = inp.text().strip()
        if not new_title:
            QMessageBox.warning(parent, "Переименование", "Имя не может быть пустым.")
            return
        ok = on_rename(old_stem, new_title)
        if ok:
            dlg.accept()

    def do_reject() -> None:
        dlg.reject()

    inp.returnPressed.connect(do_accept)

    dlg_buttons = QHBoxLayout()
    btn_cancel = QPushButton("Отмена")
    btn_ok = QPushButton("Переименовать")
    btn_ok.setDefault(True)
    btn_cancel.clicked.connect(do_reject)
    btn_ok.clicked.connect(do_accept)
    dlg_buttons.addStretch(1)
    dlg_buttons.addWidget(btn_cancel)
    dlg_buttons.addWidget(btn_ok)
    layout.addLayout(dlg_buttons)
    return dlg
