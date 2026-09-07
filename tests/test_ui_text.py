from __future__ import annotations

import pytest

from wechat_local_archive.ui_text import PRESET_LABELS, PRESET_VALUES, error_text, progress_text


def test_preset_labels_round_trip() -> None:
    assert PRESET_LABELS["balanced"] == "均衡（推荐）"
    assert {PRESET_VALUES[label] for label in PRESET_LABELS.values()} == set(PRESET_LABELS)


@pytest.mark.parametrize("source,expected", [
    ("Extracting voices", "正在提取语音…"),
    ("Extracting image 1/2", "正在提取图片 1/2"),
    ("Extracting file 2/3", "正在提取文件 2/3"),
    ("Extracted 2/3 images", "已提取图片 2/3"),
    ("Extracted 2 attachments", "已提取 2 个附件"),
    ("Extracted 2/3 voices", "已提取语音 2/3"),
    ("Decoding voice 1/2", "正在解码语音 1/2"),
    ("Transcribing batch 1/2", "正在转写第 1/2 批语音"),
    ("Transcribed 2 voice messages", "已转写 2 条语音"),
])
def test_progress_text(source: str, expected: str) -> None:
    assert progress_text(source) == expected


def test_unknown_progress_is_preserved() -> None:
    assert progress_text("Unknown source operation") == "Unknown source operation"


def test_known_errors_are_actionable_and_unknown_details_are_preserved() -> None:
    assert "YYYY-MM-DD" in error_text("Invalid date: bad. Expected YYYY-MM-DD")
    assert "初始化" in error_text("Not initialized. Run bootstrap once.")
    assert "多个" in error_text("Multiple local WeChat accounts were found; select an account first")
    assert "重新初始化" in error_text("The cached database keys no longer match the local WeChat databases.")
    assert error_text(RuntimeError("synthetic failure")) == "操作失败：synthetic failure"
