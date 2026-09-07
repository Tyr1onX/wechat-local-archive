"""Chinese presentation text for the desktop UI; core APIs keep stable values."""

from __future__ import annotations

import re

PRESET_LABELS = {
    "background": "后台（低占用）",
    "balanced": "均衡（推荐）",
    "fast": "快速（高占用）",
}
PRESET_VALUES = {label: value for value, label in PRESET_LABELS.items()}

_PROGRESS = (
    (re.compile(r"Extracting (?P<kind>image|file|video|attachment) (?P<current>\d+)/(?P<total>\d+)", re.I), "正在提取{kind} {current}/{total}"),
    (re.compile(r"Extracted (?P<current>\d+)/(?P<total>\d+) (?P<kind>images|files|videos|attachments)", re.I), "已提取{kind} {current}/{total}"),
    (re.compile(r"Decoding voice (?P<current>\d+)/(?P<total>\d+)", re.I), "正在解码语音 {current}/{total}"),
    (re.compile(r"Transcribing batch (?P<current>\d+)/(?P<total>\d+)", re.I), "正在转写第 {current}/{total} 批语音"),
)
_KINDS = {
    "image": "图片", "images": "图片", "file": "文件", "files": "文件",
    "video": "视频", "videos": "视频", "attachment": "附件", "attachments": "附件",
}


def progress_text(label: str) -> str:
    """Translate known progress labels without changing the underlying callback."""
    if label == "Extracting voices":
        return "正在提取语音…"
    match = re.fullmatch(r"Extracted (\d+)/(\d+) voices", label, re.I)
    if match:
        return f"已提取语音 {match[1]}/{match[2]}"
    match = re.fullmatch(r"Extracted (\d+) attachments", label, re.I)
    if match:
        return f"已提取 {match[1]} 个附件"
    match = re.fullmatch(r"Transcribed (\d+) voice messages", label, re.I)
    if match:
        return f"已转写 {match[1]} 条语音"
    for pattern, template in _PROGRESS:
        match = pattern.fullmatch(label)
        if match:
            values = match.groupdict()
            kind = values.get("kind", "")
            values["kind"] = _KINDS.get(kind.casefold(), kind)
            return template.format(**values)
    return label


_ERROR_PREFIXES = (
    ("Invalid date:", "日期格式不正确，请使用 YYYY-MM-DD，例如 2026-09-08。"),
    ("Not initialized.", "尚未初始化。请先登录 Windows 微信，再点击“初始化”。"),
    ("Multiple local WeChat accounts were found", "发现多个本地微信账号，请先选择要初始化的账号。"),
    ("Unable to find the local WeChat xwechat_files data root", "未找到本地微信数据目录。请确认已安装并登录 Windows 微信。"),
    ("No local WeChat 4.x account directories were found", "未找到本地微信 4.x 账号。请确认微信已经登录。"),
    ("Essential WeChat database keys could not be captured", "未能获取完整的数据库密钥。请保持已登录的微信运行后重试。"),
    ("WeChat database keys were not captured", "未能获取数据库密钥。请保持已登录的微信运行后重试。"),
    ("The cached database keys no longer match", "本地密钥与数据库不匹配。请保持微信已登录，重新初始化。"),
    ("Unable to decrypt local secret state", "无法读取本机保存的密钥。请确认使用原 Windows 用户，必要时重新初始化。"),
)


def error_text(error: BaseException | str) -> str:
    """Give actionable guidance for known failures; preserve unknown details."""
    detail = str(error)
    for prefix, message in _ERROR_PREFIXES:
        if detail.startswith(prefix):
            return message
    return f"操作失败：{detail}"
