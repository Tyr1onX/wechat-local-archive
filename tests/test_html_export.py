from __future__ import annotations

import io
import json
import re
import wave
from pathlib import Path

import pytest

from wechat_local_archive.archive import Archive, ArchiveMessage
from wechat_local_archive.cancellation import CancellationToken, ExportCancelled
from wechat_local_archive.html_export import rebuild_html, render_html, write_html
from wechat_local_archive.verify import verify_archive


def _archive(messages: list[ArchiveMessage]) -> Archive:
    return Archive(3, "2026-09-07T00:00:00+08:00", "wxid_me", "我自己", "wxid_friend", "朋友", messages)


def _message(index: int, kind: int = 1, content: str = "你好", **kwargs) -> ArchiveMessage:
    return ArchiveMessage(str(index), index, index, index, f"2026-09-07T00:{index:02d}:00+08:00", index, "我自己" if index % 2 else "朋友", "message", kind, content, **kwargs)


def _data(content: str) -> dict:
    match = re.search(r'<script id="reader-data" type="application/json">(.*?)</script>', content, re.S)
    assert match
    return json.loads(match.group(1))


def test_html_escapes_messages_and_uses_only_local_assets(tmp_path: Path) -> None:
    image = tmp_path / "assets" / "image" / "a b.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    archive = _archive([
        _message(1, content='</script><script>alert("bad")</script>'),
        _message(2, kind=3, content="<msg><img md5='secret'/></msg>", attachment="assets/image/a b.jpg"),
        _message(3, kind=34, content="<msg/>", attachment="../outside.silk"),
        _message(4, kind=49, content="<msg><appmsg><type>6</type><title>说明.pdf</title></appmsg></msg>"),
    ])
    content, warnings = render_html(archive, tmp_path, convert_audio=False)
    assert not warnings
    assert content.count("<script id=\"reader-code\">") == 1
    assert '<script>alert("bad")</script>' not in content
    assert "\\u003c/script\\u003e" in content
    rows = _data(content)["messages"]
    assert len(rows) == 4
    assert rows[0]["text"] == '</script><script>alert("bad")</script>'
    assert rows[1]["asset"] == "assets/image/a%20b.jpg"
    assert "asset" not in rows[2]
    assert rows[2]["text"] == "[语音未转写]"
    assert rows[3]["text"] == "[文件] 说明.pdf"
    assert not re.search(r"https?://", content.split('<script id="reader-code">', 1)[0], re.I)
    assert "connect-src 'none'" in content
    assert "script-src 'sha256-" in content


def test_html_is_idempotent_and_uses_committed_archive(tmp_path: Path) -> None:
    archive = _archive([_message(1)])
    (tmp_path / "archive.json").write_text(json.dumps(archive.to_dict(), ensure_ascii=False), encoding="utf-8")
    from wechat_local_archive.exporter import render_markdown
    (tmp_path / "chat.md").write_text(render_markdown(archive), encoding="utf-8")
    source = (tmp_path / "archive.json").read_bytes()
    path, warnings = rebuild_html(tmp_path)
    assert not warnings
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    rebuild_html(tmp_path)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime
    assert (tmp_path / "archive.json").read_bytes() == source
    assert verify_archive(tmp_path).ok
    path.write_text("stale", encoding="utf-8")
    assert any("chat.html is stale" in error for error in verify_archive(tmp_path).errors)
    rebuild_html(tmp_path)
    assert verify_archive(tmp_path).ok


def test_html_cancel_preserves_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "chat.html"
    target.write_bytes(b"old")
    token = CancellationToken()
    token.cancel()
    with pytest.raises(ExportCancelled):
        write_html(_archive([_message(1)]), tmp_path, cancel=token.check)
    assert target.read_bytes() == b"old"


def test_audio_cache_reuses_content_and_does_not_modify_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wechat_local_archive import html_export
    voice = tmp_path / "assets" / "voice" / "a.silk"
    voice.parent.mkdir(parents=True)
    voice.write_bytes(b"silk")
    calls: list[Path] = []

    def fake_convert(source, destination, cancel=None):
        calls.append(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"MP3")

    monkeypatch.setattr(html_export, "_convert_audio", fake_convert)
    archive = _archive([_message(1, 34, "", attachment="assets/voice/a.silk", transcript="语音内容")])
    first, _ = write_html(archive, tmp_path)
    rows = _data(first.read_text(encoding="utf-8"))["messages"]
    audio = tmp_path / rows[0]["audio"]
    assert audio.read_bytes() == b"MP3"
    mtime = audio.stat().st_mtime_ns
    write_html(archive, tmp_path)
    assert len(calls) == 1
    assert audio.stat().st_mtime_ns == mtime
    assert voice.read_bytes() == b"silk"
    assert _data(first.read_text(encoding="utf-8"))["messages"][0]["audio"] == rows[0]["audio"]
    offline, _ = render_html(archive, tmp_path, convert_audio=False)
    assert _data(offline)["messages"][0]["audio"] == rows[0]["audio"]
    assert len(calls) == 1


def test_audio_decode_failure_keeps_transcript_and_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wechat_local_archive import html_export
    voice = tmp_path / "assets" / "voice" / "a.silk"
    voice.parent.mkdir(parents=True)
    voice.write_bytes(b"silk")
    monkeypatch.setattr(html_export, "_convert_audio", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad audio")))
    archive = _archive([_message(1, 34, "", attachment="assets/voice/a.silk", transcript="你好")])
    content, warnings = render_html(archive, tmp_path)
    assert len(warnings) == 1
    row = _data(content)["messages"][0]
    assert row["text"] == "你好"
    assert row["asset"] == "assets/voice/a.silk"
    assert "audio" not in row


def test_real_silk_conversion_produces_browser_audio(tmp_path: Path) -> None:
    pytest.importorskip("pysilk")
    from wechat_local_archive.html_export import _convert_audio
    # A valid short SILK sample is decoded by the same local library as the ASR path.
    import pysilk
    pcm = b"\x00\x00" * 24000
    source = tmp_path / "sample.silk"
    with source.open("wb") as output:
        pysilk.encode(io.BytesIO(pcm), output, 24000, 24000)
    target = tmp_path / "sample.mp3"
    _convert_audio(source, target)
    assert target.stat().st_size > 0
    assert target.read_bytes()[:3] == b"ID3" or target.read_bytes()[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}
