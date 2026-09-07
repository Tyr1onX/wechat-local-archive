from __future__ import annotations

from wechat_local_archive.cli import build_parser, main


def test_html_cli_accepts_offline_rebuild_options() -> None:
    args = build_parser().parse_args(["html", "archive", "--no-audio"])
    assert str(args.archive_dir) == "archive"
    assert args.no_audio is True


def test_export_cli_defaults_to_balanced_asr() -> None:
    args = build_parser().parse_args(["export", "friend", "--out", "archive", "--transcribe"])
    assert args.asr_preset == "balanced"
    assert args.batch_size is None


def test_export_cli_accepts_resource_preset_and_batch_override() -> None:
    args = build_parser().parse_args(
        [
            "export",
            "friend",
            "--out",
            "archive",
            "--transcribe",
            "--asr-preset",
            "background",
            "--batch-size",
            "11",
        ]
    )
    assert args.asr_preset == "background"
    assert args.batch_size == 11


def test_review_cli_defaults_to_safe_modes() -> None:
    args = build_parser().parse_args(["retranscribe", "archive", "--id", "123"])
    assert args.model == "small"
    assert args.limit == 20
    assert not args.apply_review
    assert not args.download_model
    assert build_parser().parse_args(["export", "friend", "--out", "archive"]).voice_quality == "fast"


def test_retranscribe_cli_uses_archive_only(monkeypatch) -> None:
    from wechat_local_archive.quality import ReviewSummary
    captured = {}
    def fake(root, **kwargs):
        captured.update(kwargs)
        return ReviewSummary(1, 1, 0, True, ())
    monkeypatch.setattr("wechat_local_archive.cli.review_archive", fake)
    monkeypatch.setattr("wechat_local_archive.cli.open_offline", lambda: (_ for _ in ()).throw(AssertionError("WeChat opened")))
    assert main(["retranscribe", "archive", "--id", "123"]) == 0
    assert captured["ids"] == ("123",)
    assert not captured["apply"]
    assert main(["retranscribe", "archive", "--id", "123", "--all-suspicious"]) == 2
