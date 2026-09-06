from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Event

import pytest

from wechat_local_archive.exporter import ExportSummary
from wechat_local_archive.service import ServiceStatus
from wechat_local_archive.verify import VerifyResult


class FakeService:
    def __init__(self, root: Path, ready: bool = True) -> None:
        self.root = root
        self.ready = ready
        self.chats = [{"username": "wxid_friend", "name": "朋友", "message_count": 2}]
        self.entered = Event()
        self.block = False
        self.exports = []

    def status(self):
        return ServiceStatus(ready=self.ready, account="wxid_test" if self.ready else "")

    def accounts(self):
        return [{"account": "wxid_test"}]

    def initialize(self, account=None):
        self.ready = True
        return self.status()

    def list_chats(self):
        return self.chats

    def destination(self, root, chat):
        return Path(root) / chat["username"]

    def export(self, **kwargs):
        self.exports.append(kwargs)
        self.entered.set()
        check = kwargs["cancel"]
        progress = kwargs["progress"]
        if self.block:
            while True:
                check()
                time.sleep(0.01)
        progress(1, 2, "Exporting 1/2")
        check()
        directory = kwargs["output_dir"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "archive.json").write_text("{}", encoding="utf-8")
        (directory / "chat.md").write_text("test", encoding="utf-8")
        return ExportSummary(directory / "archive.json", directory / "chat.md", 2, 0, 0, ())

    def verify(self, root):
        return VerifyResult(Path(root), 2, 0, 0, 0, 0, 0, (), ())


def _wait(root, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("GUI operation did not finish")


@pytest.fixture(scope="module")
def tk_root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


@pytest.fixture
def window(tmp_path, monkeypatch, tk_root):
    import tkinter as tk
    import wechat_local_archive.gui as gui

    monkeypatch.setattr(gui, "load_ui_config", lambda: None)
    monkeypatch.setattr(gui, "save_ui_config", lambda config: None)
    root = tk.Toplevel(tk_root)
    root.withdraw()
    service = FakeService(tmp_path)
    app = gui.ArchiveWindow(root, service=service)
    app.output.set(str(tmp_path))
    try:
        _wait(root, lambda: not app._busy)
        yield root, app, service
    finally:
        if app.worker.running:
            app.worker.cancel()
            app.worker.join()
        if root.winfo_exists():
            app.close()
            root.update()


def test_single_window_load_export_verify_and_validation(window, tmp_path):
    root, app, service = window
    assert app._ready
    assert app.account_text.get() == "wxid_test"
    app.search.set("朋友")
    root.update()
    assert app.chat_tree.selection() == ("wxid_friend",)
    app.start_date.set("bad")
    app.start_export()
    assert not app._busy
    assert not service.exports
    app.start_date.set("2026-01-01")
    app.end_date.set("2026-01-02")
    app.preset.set("background")
    app.start_export()
    _wait(root, lambda: not app._busy)
    assert service.exports[0]["asr_preset"] == "background"
    assert service.exports[0]["media_types"] == frozenset({3, 34, 43, 49})
    assert service.exports[0]["start"].isoformat() == "2026-01-01"
    assert app._last_directory == tmp_path / "wxid_friend"
    assert "DONE" in app.status_text.get()
    app.start_verify()
    _wait(root, lambda: not app._busy)
    assert "PASS" in app.status_text.get()


def test_cancel_and_close_wait_for_worker(window):
    root, app, service = window
    app.search.set("朋友")
    root.update()
    service.block = True
    app.start_export()
    assert service.entered.wait(5)
    app.cancel()
    _wait(root, lambda: not app._busy)
    assert "已取消" in app.status_text.get()
    assert not app.worker.running
    app.start_export()
    _wait(root, lambda: app.worker.running)
    app.close()
    _wait(root, lambda: not root.winfo_exists())
    assert not app.worker.running


def test_initialize_stays_in_same_window(tmp_path, monkeypatch, tk_root):
    import tkinter as tk
    import wechat_local_archive.gui as gui

    monkeypatch.setattr(gui, "load_ui_config", lambda: None)
    monkeypatch.setattr(gui, "save_ui_config", lambda config: None)
    root = tk.Toplevel(tk_root)
    root.withdraw()
    service = FakeService(tmp_path, ready=False)
    app = gui.ArchiveWindow(root, service=service)
    app.output.set(str(tmp_path))
    try:
        _wait(root, lambda: not app._busy)
        assert not app._ready
        assert app.setup_frame.winfo_ismapped() or app.setup_frame.winfo_manager() == "grid"
        app.initialize()
        _wait(root, lambda: not app._busy)
        assert app._ready
        assert service.ready
    finally:
        app.close()
        root.update()


@pytest.mark.parametrize("scale", [1.25, 1.5])
def test_gui_dpi_layout_smoke(window, scale):
    root, app, _service = window
    previous_scale = root.tk.call("tk", "scaling")
    try:
        root.tk.call("tk", "scaling", scale)
        root.deiconify()
        root.geometry("820x700")
        root.update()
        assert app.ready_frame.winfo_width() > 0
        assert app.export_button.winfo_ismapped()
        assert app.open_button.winfo_ismapped()
        assert app.ready_frame.winfo_reqwidth() <= root.winfo_width()
        root.geometry("600x520")
        root.update()
        assert app.form_canvas.winfo_height() > 0
        assert app.form_scroll.winfo_ismapped()
        assert app.open_button.winfo_ismapped()
    finally:
        root.withdraw()
        root.tk.call("tk", "scaling", previous_scale)
