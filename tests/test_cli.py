from __future__ import annotations

from wechat_local_archive.cli import build_parser


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
