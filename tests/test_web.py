from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wechat_local_archive.exporter import ExportSummary
from wechat_local_archive.service import ServiceStatus
from wechat_local_archive.state import UiConfig
from wechat_local_archive.verify import VerifyResult
from wechat_local_archive.web import WebApplication, create_server


class FakeService:
    def __init__(self, root: Path, *, ready: bool = True) -> None:
        self.root = root
        self.ready = ready
        self.chats = [
            {"username": "wxid_friend", "name": "朋友", "message_count": 12, "account_id": "wxid_me"},
            {"username": "wxid_other", "name": "同学", "message_count": 4, "account_id": "wxid_me"},
        ]
        self.initialized: list[str | None] = []
        self.exports: list[dict] = []
        self.entered = Event()
        self.block = False
        self.fail_initialize = False
        self.fail_export = False
        self.fail_verify = False
        self.html_calls: list[Path] = []

    def status(self):
        return ServiceStatus(ready=self.ready, account="wxid_me" if self.ready else "", data_root="C:/wechat")

    def accounts(self):
        return [{"account": "wxid_me"}, {"account": "wxid_alt"}]

    def initialize(self, account=None):
        self.initialized.append(account)
        if self.fail_initialize:
            raise RuntimeError("bootstrap failed")
        self.ready = True
        return self.status()

    def list_chats(self):
        return self.chats

    def destination(self, root, chat):
        return Path(root) / str(chat["username"])

    def export(self, **kwargs):
        self.exports.append(kwargs)
        self.entered.set()
        progress = kwargs["progress"]
        check = kwargs["cancel"]
        progress(1, 2, "Transcribing batch 1/2")
        if self.fail_export:
            raise RuntimeError("export failed")
        if self.block:
            while True:
                check()
                time.sleep(0.01)
        check()
        target = Path(kwargs["output_dir"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "archive.json").write_text("{}", encoding="utf-8")
        (target / "chat.md").write_text("test", encoding="utf-8")
        return ExportSummary(target / "archive.json", target / "chat.md", 12, 3, 2, ())

    def html(self, root):
        root = Path(root)
        self.html_calls.append(root)
        path = root / "chat.html"
        path.write_text("<html>chat</html>", encoding="utf-8")
        return path, ()

    def verify(self, root):
        if self.fail_verify:
            raise RuntimeError("verify failed")
        return VerifyResult(Path(root), 12, 3, 2, 2, 0, 0, (), ())


@pytest.fixture
def web_server(tmp_path, monkeypatch):
    import wechat_local_archive.web as web

    service = FakeService(tmp_path)
    opened: list[Path] = []
    saved: list[UiConfig] = []
    settings = UiConfig(output_root=str(tmp_path), asr_preset="balanced")
    monkeypatch.setattr(web, "load_ui_config", lambda: settings)
    monkeypatch.setattr(web, "save_ui_config", saved.append)

    app = WebApplication(service=service, opener=lambda path: opened.append(Path(path)))
    server = create_server(app, start_port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, app, service, opened, saved, server
    finally:
        if app.worker.running:
            app.worker.cancel()
            app.worker.join()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(base: str, path: str, *, method: str = "GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = {} if headers is None else dict(headers)
    if data is not None:
        request_headers.setdefault("Content-Type", "application/json")
    req = Request(base + path, data=data, headers=request_headers, method=method)
    try:
        with urlopen(req, timeout=5) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            payload = json.loads(raw) if content_type.startswith("application/json") else raw.decode("utf-8")
            return response.status, payload
    except HTTPError as exc:
        raw = exc.read()
        content_type = exc.headers.get("Content-Type", "")
        payload = json.loads(raw) if raw and content_type.startswith("application/json") else raw.decode("utf-8")
        return exc.code, payload


def wait_task(base: str, state: str | set[str], timeout: float = 5):
    states = {state} if isinstance(state, str) else state
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        status, last = request(base, "/api/task")
        assert status == 200
        if last["state"] in states:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task did not reach {states}: {last}")


def test_status_chats_search_static_and_loopback(web_server):
    base, _app, _service, _opened, _saved, server = web_server
    assert server.server_address[0] == "127.0.0.1"

    status, page = request(base, "/")
    assert status == 200
    assert "微信本地归档" in page

    status, payload = request(base, "/api/status")
    assert status == 200
    assert payload["ready"] is True
    assert payload["account"] == "wxid_me"

    status, payload = request(base, "/api/chats?search=%E6%9C%8B%E5%8F%8B")
    assert status == 200
    assert [chat["username"] for chat in payload["chats"]] == ["wxid_friend"]

    status, payload = request(base, "/api/chats?search=wxid_other")
    assert status == 200
    assert [chat["name"] for chat in payload["chats"]] == ["同学"]


def test_bootstrap_success_and_failure(web_server):
    base, _app, service, _opened, _saved, _server = web_server
    service.ready = False

    status, payload = request(base, "/api/bootstrap", method="POST", body={"account": "wxid_alt"})
    assert status == 202
    assert payload["state"] == "running"
    done = wait_task(base, "done")
    assert done["task"] == "bootstrap"
    assert service.initialized == ["wxid_alt"]
    assert service.ready

    service.ready = False
    service.fail_initialize = True
    status, _payload = request(base, "/api/bootstrap", method="POST", body={"account": "wxid_me"})
    assert status == 202
    failed = wait_task(base, "error")
    assert "bootstrap failed" in failed["error"]


def test_export_progress_duplicate_cancel_done_and_error(web_server):
    base, _app, service, _opened, saved, _server = web_server
    service.block = True
    service.entered.clear()

    status, payload = request(
        base,
        "/api/export",
        method="POST",
        body={
            "chat": "wxid_friend",
            "start": "2026-01-01",
            "end": "2026-01-02",
            "images": True,
            "voice": True,
            "other_media": False,
            "transcribe": True,
            "asr_preset": "background",
        },
    )
    assert status == 202
    assert payload["task"] == "export"
    assert service.entered.wait(5)

    running = wait_task(base, "running")
    assert running["current"] == 1
    assert running["total"] == 2
    assert "正在转写第 1/2 批语音" == running["label"]

    status, payload = request(base, "/api/export", method="POST", body={"chat": "wxid_friend"})
    assert status == 409
    assert "正在运行" in payload["error"]

    status, payload = request(base, "/api/task/cancel", method="POST", body={})
    assert status == 200
    assert payload["cancelling"] is True
    cancelled = wait_task(base, "cancelled")
    assert cancelled["task"] == "export"

    service.block = False
    service.entered.clear()
    status, _payload = request(base, "/api/export", method="POST", body={"chat": "wxid_friend"})
    assert status == 202
    done = wait_task(base, "done")
    assert done["result"]["message_count"] == 12
    assert done["result"]["attachment_count"] == 3
    assert service.html_calls[-1] == Path(saved[-1].output_root) / "wxid_friend"
    assert service.exports[-1]["media_types"] == frozenset({3, 34, 43, 49})
    assert saved[0].asr_preset == "background"

    service.fail_export = True
    service.entered.clear()
    status, _payload = request(base, "/api/export", method="POST", body={"chat": "wxid_friend"})
    assert status == 202
    failed = wait_task(base, "error")
    assert "export failed" in failed["error"]


def test_export_validation_verify_and_result_actions(web_server):
    base, _app, service, opened, _saved, _server = web_server

    status, payload = request(
        base,
        "/api/export",
        method="POST",
        body={"chat": "wxid_friend", "start": "2026-02-02", "end": "2026-01-01"},
    )
    assert status == 400
    assert "开始日期" in payload["error"]

    status, _payload = request(base, "/api/export", method="POST", body={"chat": "wxid_friend"})
    assert status == 202
    wait_task(base, "done")

    status, _payload = request(base, "/api/verify", method="POST", body={"chat": "wxid_friend"})
    assert status == 202
    verified = wait_task(base, "done")
    assert verified["task"] == "verify"
    assert verified["result"]["voice_transcript_count"] == 2

    status, payload = request(base, "/api/open-folder", method="POST", body={"chat": "wxid_friend"})
    assert status == 200
    assert payload["ok"] is True
    assert opened[-1].name == "wxid_friend"

    status, payload = request(base, "/api/open-chat", method="POST", body={"chat": "wxid_friend"})
    assert status == 200
    assert payload["ok"] is True
    assert opened[-1].name == "chat.html"

    service.fail_verify = True
    status, _payload = request(base, "/api/verify", method="POST", body={"chat": "wxid_friend"})
    assert status == 202
    failed = wait_task(base, "error")
    assert "verify failed" in failed["error"]


def test_request_boundary_rejects_nonlocal_host_and_non_json_post(web_server):
    base, _app, _service, _opened, _saved, _server = web_server

    status, payload = request(base, "/api/status", headers={"Host": "example.invalid"})
    assert status == 400
    assert "本机" in payload["error"]

    req = Request(base + "/api/task/cancel", data=b"{}", headers={"Content-Type": "text/plain"}, method="POST")
    with pytest.raises(HTTPError) as exc:
        urlopen(req, timeout=5)
    assert exc.value.code == 415
