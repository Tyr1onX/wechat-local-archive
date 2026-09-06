from __future__ import annotations

from threading import Event


class ExportCancelled(RuntimeError):
    """A user-requested stop, not an export failure."""


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise ExportCancelled("Export cancelled")
