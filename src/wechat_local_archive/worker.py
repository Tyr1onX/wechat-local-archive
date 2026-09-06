from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread
from typing import Callable

from .cancellation import CancellationToken, ExportCancelled


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    kind: str
    value: object = None


class ArchiveWorker:
    """One background operation. The UI alone consumes events and owns widgets."""

    def __init__(self) -> None:
        self.events: Queue[WorkerEvent] = Queue()
        self._thread: Thread | None = None
        self._token: CancellationToken | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, operation: Callable[[Callable[[], None], Callable[[int, int, str], None]], object]) -> None:
        if self.running:
            raise RuntimeError("An operation is already running")
        self._token = CancellationToken()
        token = self._token
        self.events = Queue()

        def progress(current: int, total: int, label: str) -> None:
            token.check()
            self.events.put(WorkerEvent("progress", (current, total, label)))

        def run() -> None:
            try:
                token.check()
                result = operation(token.check, progress)
                # A late cancel must not turn a successfully committed export
                # into a false cancellation result.
            except ExportCancelled:
                self.events.put(WorkerEvent("cancelled"))
            except Exception as exc:
                self.events.put(WorkerEvent("error", str(exc)))
            else:
                self.events.put(WorkerEvent("done", result))

        # Non-daemon: closing the window must not abandon an active archive write.
        self._thread = Thread(target=run, name="wechat-archive-worker", daemon=False)
        self._thread.start()

    def cancel(self) -> None:
        if self._token is not None:
            self._token.cancel()

    def join(self) -> None:
        if self._thread is not None:
            self._thread.join()

    def drain(self) -> list[WorkerEvent]:
        result: list[WorkerEvent] = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except Empty:
                return result
