from __future__ import annotations

from threading import Event

import pytest

from wechat_local_archive.cancellation import CancellationToken, ExportCancelled
from wechat_local_archive.worker import ArchiveWorker, WorkerEvent


def test_cancellation_token() -> None:
    token = CancellationToken()
    token.check()
    token.cancel()
    assert token.cancelled
    with pytest.raises(ExportCancelled):
        token.check()


def test_worker_serializes_operations_and_reports_cancellation() -> None:
    worker = ArchiveWorker()
    entered = Event()
    release = Event()

    def blocked(check, progress):
        entered.set()
        assert release.wait(5)
        progress(1, 2, "first")
        check()
        return "unexpected"

    worker.start(blocked)
    assert entered.wait(5)
    with pytest.raises(RuntimeError, match="already running"):
        worker.start(blocked)
    worker.cancel()
    release.set()
    worker.join()
    events = worker.drain()
    assert events[-1].kind == "cancelled"
    assert all(event.kind != "done" for event in events)

    worker.start(lambda check, progress: "next")
    worker.join()
    assert worker.drain()[-1].value == "next"


def test_late_cancel_does_not_hide_successful_commit() -> None:
    worker = ArchiveWorker()

    def committed(check, progress):
        worker.cancel()
        return "saved"

    worker.start(committed)
    worker.join()
    assert worker.drain()[-1] == WorkerEvent("done", "saved")


def test_worker_reports_real_error() -> None:
    worker = ArchiveWorker()

    def fail(check, progress):
        raise ValueError("test failure")

    worker.start(fail)
    worker.join()
    event = worker.drain()[-1]
    assert event.kind == "error"
    assert event.value == "test failure"
