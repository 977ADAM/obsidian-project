from collections import deque
from collections.abc import Callable
from typing import TypeAlias

OpenCallbackResult: TypeAlias = bool | None
OpenCallback: TypeAlias = Callable[[str], OpenCallbackResult]


class NavigationController:
    """
    Управляет историей навигации заметок (back / forward).
    Не знает ничего про UI или файлы — только заголовки.
    """

    def __init__(self, open_callback: OpenCallback, *, history_limit: int | None = None) -> None:
        if history_limit is not None and history_limit < 0:
            raise ValueError("history_limit must be >= 0 or None")

        # Callback, который реально открывает заметку во "внешнем мире" (UI/файлы).
        self._open_callback: OpenCallback = open_callback
        self._back: deque[str] = deque(maxlen=history_limit)
        self._forward: deque[str] = deque(maxlen=history_limit)
        self._current: str | None = None

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Привести заголовок к каноническому виду (обрезать пробелы)."""
        return title.strip()

    @property
    def current(self) -> str | None:
        """Текущий открытый заголовок (или None, если ничего не открыто)."""
        return self._current

    @property
    def can_back(self) -> bool:
        """Есть ли куда перейти назад."""
        return self._current is not None and bool(self._back)

    @property
    def can_forward(self) -> bool:
        """Есть ли куда перейти вперёд."""
        return self._current is not None and bool(self._forward)

    def _try_open(self, title: str) -> bool:
        """
        Вызывает callback открытия и интерпретирует результат.

        Считаем успехом:
          - None (callback ничего не вернул)
          - True
        Неуспех:
          - False (состояние/история не меняются)
        Исключения пробрасываются наружу.
        """
        result = self._open_callback(title)
        # Успех: callback вернул None или True.
        # Неуспех: callback вернул False.
        if result is None or result is True:
            return True
        if result is False:
            return False
        raise TypeError(
            "open_callback must return bool | None (got "
            f"{type(result).__name__}: {result!r})"
        )

    def open(self, title: str, *, reopen_current: bool = True) -> bool:
        """
        Открыть заметку по заголовку.

        - Если title пустой/из пробелов — ничего не делает.
        - Если title совпадает с текущим:
            - reopen_current=True  -> вызовет callback (как "refresh")
            - reopen_current=False -> no-op

        Возвращает True, если открытие было закоммичено (callback успешен).
        """
        normalized_title = self._normalize_title(title)
        if not normalized_title:
            return False

        current = self._current
        if current == normalized_title:
            if not reopen_current:
                return False
            # Повторное открытие текущей заметки (refresh) — историю не трогаем.
            return self._try_open(normalized_title)

        # Важно: сначала пробуем открыть в "мире" (UI/файлы),
        # и только после успешного callback коммитим состояние.
        if not self._try_open(normalized_title):
            return False

        if current is not None:
            self._back.append(current)
            self._forward.clear()

        self._current = normalized_title
        return True

    def back(self) -> bool:
        """Переход назад. Возвращает True, если переход произошёл."""
        return self._navigate(self._back, self._forward)

    def forward(self) -> bool:
        """Переход вперёд. Возвращает True, если переход произошёл."""
        return self._navigate(self._forward, self._back)

    def _navigate(self, source: deque[str], target: deque[str]) -> bool:
        current = self._current
        if not source or current is None:
            return False

        new_current = source[-1]

        # Транзакционность: сначала callback, потом мутация истории.
        if not self._try_open(new_current):
            return False

        target.append(current)
        source.pop()
        self._current = new_current
        return True

    def clear(self) -> None:
        self._back.clear()
        self._forward.clear()
        self._current = None
