from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import webbrowser
from dataclasses import asdict, is_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from threading import RLock, Thread
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from .archive import parse_date
from .service import ArchiveService
from .state import UiConfig, load_ui_config, save_ui_config
from .ui_text import PRESET_LABELS, error_text, progress_text
from .worker import ArchiveWorker


LOG = logging.getLogger(__name__)
HOST = "127.0.0.1"
DEFAULT_PORT = 40654
MAX_BODY = 64 * 1024


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class WebApplication:
    """Thin HTTP-facing adapter over ArchiveService and ArchiveWorker."""

    def __init__(
        self,
        service: ArchiveService | None = None,
        worker: ArchiveWorker | None = None,
        *,
        opener: Callable[[Path], None] | None = None,
    ) -> None:
        self.service = service or ArchiveService()
        self.worker = worker or ArchiveWorker()
        self._opener = opener or _open_local_path
        self._lock = RLock()
        self._task: dict[str, Any] = self._idle_task()

    @staticmethod
    def _idle_task() -> dict[str, Any]:
        return {
            "state": "idle",
            "task": None,
            "current": 0,
            "total": 0,
            "label": "",
            "error": None,
            "result": None,
            "cancelling": False,
        }

    def status(self) -> dict[str, Any]:
        status = self.service.status()
        settings = load_ui_config()
        return {
            **asdict(status),
            "output_root": str(self._output_root(settings)),
            "asr_preset": settings.asr_preset if settings else "balanced",
        }

    def accounts(self) -> list[dict]:
        return self.service.accounts()

    def chats(self, search: str = "") -> list[dict]:
        needle = search.strip().casefold()
        chats = self.service.list_chats()
        if not needle:
            return chats
        return [
            chat
            for chat in chats
            if needle in str(chat.get("name") or "").casefold()
            or needle in str(chat.get("username") or "").casefold()
        ]

    def bootstrap(self, body: dict[str, Any]) -> dict[str, Any]:
        account = str(body.get("account") or "").strip() or None

        def operation(check, progress):
            check()
            status = self.service.initialize(account=account)
            check()
            return status

        return self._start_task("bootstrap", operation)

    def export(self, body: dict[str, Any]) -> dict[str, Any]:
        username = self._required_chat(body)
        chat = self._find_chat(username)
        try:
            start = parse_date(str(body.get("start") or "").strip())
            end = parse_date(str(body.get("end") or "").strip())
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, error_text(exc)) from exc
        if start and end and start > end:
            raise ApiError(HTTPStatus.BAD_REQUEST, "开始日期不能晚于结束日期")

        images = _bool(body, "images", True)
        voice = _bool(body, "voice", True)
        other_media = _bool(body, "other_media", True)
        transcribe = _bool(body, "transcribe", True) and voice
        preset = str(body.get("asr_preset") or "balanced")
        if preset not in PRESET_LABELS:
            raise ApiError(HTTPStatus.BAD_REQUEST, "未知的语音转写档位")

        settings = load_ui_config()
        output_root = self._output_root(settings)
        save_ui_config(UiConfig(output_root=str(output_root), asr_preset=preset))
        target = self.service.destination(output_root, chat)
        media_types = frozenset(
            ({3} if images else set())
            | ({34} if voice else set())
            | ({43, 49} if other_media else set())
        )

        def operation(check, progress):
            return self.service.export(
                chat_selector=username,
                output_dir=target,
                start=start,
                end=end,
                media_types=media_types,
                transcribe=transcribe,
                asr_preset=preset,
                progress=progress,
                cancel=check,
            )

        return self._start_task("export", operation)

    def verify(self, body: dict[str, Any]) -> dict[str, Any]:
        username = self._required_chat(body)
        target = self.service.destination(self._output_root(load_ui_config()), self._find_chat(username))

        def operation(check, progress):
            check()
            result = self.service.verify(target)
            check()
            return result

        return self._start_task("verify", operation)

    def task(self) -> dict[str, Any]:
        with self._lock:
            self._drain_worker()
            return dict(self._task)

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            self._drain_worker()
            if not self.worker.running:
                raise ApiError(HTTPStatus.CONFLICT, "当前没有正在运行的任务")
            self.worker.cancel()
            self._task["cancelling"] = True
            self._task["label"] = "正在取消，等待当前操作完成…"
            return dict(self._task)

    def close(self) -> None:
        with self._lock:
            if self.worker.running:
                self.worker.cancel()
        self.worker.join()

    def open_folder(self, body: dict[str, Any]) -> dict[str, Any]:
        root = self._output_root(load_ui_config())
        username = str(body.get("chat") or "").strip()
        if username:
            target = self.service.destination(root, self._find_chat(username))
            self._ensure_inside(root, target)
            if not target.is_dir():
                raise ApiError(HTTPStatus.NOT_FOUND, "所选会话还没有归档")
        else:
            root.mkdir(parents=True, exist_ok=True)
            target = root
        self._opener(target)
        return {"ok": True}

    def open_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        username = self._required_chat(body)
        root = self._output_root(load_ui_config())
        target = self.service.destination(root, self._find_chat(username))
        self._ensure_inside(root, target)
        chat_html = target / "chat.html"
        if not chat_html.is_file():
            chat_html, _warnings = self.service.html(target)
        self._opener(chat_html)
        return {"ok": True}

    def _start_task(self, kind: str, operation) -> dict[str, Any]:
        with self._lock:
            self._drain_worker()
            if self.worker.running:
                raise ApiError(HTTPStatus.CONFLICT, "已有任务正在运行")
            self._task = {
                "state": "running",
                "task": kind,
                "current": 0,
                "total": 0,
                "label": {
                    "bootstrap": "正在初始化…",
                    "export": "正在归档…",
                    "verify": "正在检查归档…",
                }.get(kind, "正在处理…"),
                "error": None,
                "result": None,
                "cancelling": False,
            }
            try:
                self.worker.start(operation)
            except RuntimeError as exc:
                self._task = self._idle_task()
                raise ApiError(HTTPStatus.CONFLICT, error_text(exc)) from exc
            return dict(self._task)

    def _drain_worker(self) -> None:
        for event in self.worker.drain():
            if event.kind == "progress":
                current, total, label = event.value
                self._task.update(
                    current=int(current),
                    total=int(total),
                    label=progress_text(str(label)),
                )
                continue
            self.worker.join()
            self._task["cancelling"] = False
            if event.kind == "done":
                self._task["state"] = "done"
                self._task["result"] = _jsonable(event.value)
                self._task["label"] = {
                    "bootstrap": "初始化完成",
                    "export": "归档完成",
                    "verify": "检查完成",
                }.get(str(self._task.get("task")), "处理完成")
            elif event.kind == "cancelled":
                self._task["state"] = "cancelled"
                self._task["label"] = "已取消"
            else:
                self._task["state"] = "error"
                self._task["error"] = error_text(event.value)
                self._task["label"] = ""

    def _required_chat(self, body: dict[str, Any]) -> str:
        username = str(body.get("chat") or "").strip()
        if not username:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请先选择会话")
        return username

    def _find_chat(self, username: str) -> dict:
        for chat in self.service.list_chats():
            if str(chat.get("username") or "") == username:
                return chat
        raise ApiError(HTTPStatus.NOT_FOUND, "未找到所选会话")

    @staticmethod
    def _output_root(settings: UiConfig | None) -> Path:
        value = settings.output_root if settings else str(Path.home() / "Downloads" / "wechat-local-archive")
        return Path(value).expanduser().resolve()

    @staticmethod
    def _ensure_inside(root: Path, target: Path) -> None:
        root = root.resolve()
        target = target.resolve()
        if not target.is_relative_to(root):
            raise ApiError(HTTPStatus.BAD_REQUEST, "归档路径无效")


def _bool(body: dict[str, Any], key: str, default: bool) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{key} 必须是布尔值")
    return value


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _open_local_path(path: Path) -> None:
    path = path.resolve()
    if os.name == "nt":
        os.startfile(str(path))
        return
    command = ["open", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path)]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class ArchiveRequestHandler(BaseHTTPRequestHandler):
    app: WebApplication
    static_root = resources.files("wechat_local_archive").joinpath("web")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            self._validate_host()
            if parsed.path == "/api/status":
                return self._json(HTTPStatus.OK, self.app.status())
            if parsed.path == "/api/accounts":
                return self._json(HTTPStatus.OK, {"accounts": self.app.accounts()})
            if parsed.path == "/api/chats":
                query = parse_qs(parsed.query)
                return self._json(HTTPStatus.OK, {"chats": self.app.chats(query.get("search", [""])[0])})
            if parsed.path == "/api/task":
                return self._json(HTTPStatus.OK, self.app.task())
            if parsed.path in {"/", "/index.html", "/app.js", "/style.css"}:
                return self._static("index.html" if parsed.path == "/" else parsed.path.removeprefix("/"))
            if parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            raise ApiError(HTTPStatus.NOT_FOUND, "页面不存在")
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive server boundary
            LOG.exception("GET %s failed", parsed.path)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": error_text(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            self._validate_host()
            body = self._body()
            if parsed.path == "/api/shutdown":
                self._json(HTTPStatus.OK, {"ok": True})
                Thread(target=self._shutdown_server, name="wechat-archive-shutdown", daemon=False).start()
                return
            routes = {
                "/api/bootstrap": self.app.bootstrap,
                "/api/export": self.app.export,
                "/api/task/cancel": lambda _body: self.app.cancel(),
                "/api/verify": self.app.verify,
                "/api/open-folder": self.app.open_folder,
                "/api/open-chat": self.app.open_chat,
            }
            action = routes.get(parsed.path)
            if action is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "接口不存在")
            status = HTTPStatus.ACCEPTED if parsed.path in {"/api/bootstrap", "/api/export", "/api/verify"} else HTTPStatus.OK
            self._json(status, action(body))
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive server boundary
            LOG.exception("POST %s failed", parsed.path)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": error_text(exc)})

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求格式无效") from exc
        if length > MAX_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求内容过大")
        if length == 0:
            return {}
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "接口只接受 JSON 请求")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求格式无效") from exc
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求格式无效")
        return value

    def _shutdown_server(self) -> None:
        try:
            self.app.close()
        finally:
            self.server.shutdown()

    def _validate_host(self) -> None:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").casefold()
        if host not in {"127.0.0.1", "localhost"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "只允许本机访问")

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(_jsonable(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, name: str) -> None:
        if name not in {"index.html", "app.js", "style.css"}:
            raise ApiError(HTTPStatus.NOT_FOUND, "页面不存在")
        item = self.static_root.joinpath(name)
        try:
            data = item.read_bytes()
        except (FileNotFoundError, OSError) as exc:
            raise ApiError(HTTPStatus.NOT_FOUND, "页面资源缺失") from exc
        content_type = {
            "index.html": "text/html; charset=utf-8",
            "app.js": "text/javascript; charset=utf-8",
            "style.css": "text/css; charset=utf-8",
        }[name]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        LOG.debug(format, *args)


def create_server(
    app: WebApplication | None = None,
    *,
    host: str = HOST,
    start_port: int = DEFAULT_PORT,
    attempts: int = 50,
) -> ThreadingHTTPServer:
    if host != HOST:
        raise ValueError("Web UI may only bind to 127.0.0.1")
    application = app or WebApplication()

    class Handler(ArchiveRequestHandler):
        app = application

    last_error: OSError | None = None
    for port in range(start_port, start_port + attempts):
        try:
            server = ThreadingHTTPServer((host, port), Handler)
            server.daemon_threads = True
            return server
        except OSError as exc:
            last_error = exc
    raise RuntimeError("无法找到可用的本地端口") from last_error


def main(*, smoke: bool = False, open_browser: bool = True) -> int:
    app = WebApplication()
    server = create_server(app)
    url = f"http://{HOST}:{server.server_address[1]}/"
    if smoke:
        thread = Thread(target=server.serve_forever, name="wechat-archive-web-smoke", daemon=True)
        thread.start()
        try:
            with urlopen(url, timeout=5) as response:
                if response.status != HTTPStatus.OK or b"<title>" not in response.read():
                    raise RuntimeError("Web smoke page failed")
            with urlopen(url + "api/status", timeout=5) as response:
                if response.status != HTTPStatus.OK:
                    raise RuntimeError(f"Web smoke status failed: {response.status}")
        finally:
            app.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        return 0

    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
