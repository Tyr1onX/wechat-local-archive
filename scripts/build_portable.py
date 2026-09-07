"""Build the Windows onedir package and a traceable, deterministic-layout ZIP."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "wechat-local-archive-windows-x64"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(out_dir: Path) -> Path:
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Portable builds require 64-bit Windows")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Portable builds require Python 3.12")
    version = metadata.version("wechat-local-archive")
    expected = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if version != expected:
        raise RuntimeError(f"Expected project version {expected}, got {version}; reinstall the project")
    if importlib.util.find_spec("faster_whisper") is not None:
        raise RuntimeError("Use a clean base build environment without optional Whisper")
    lock_text = (ROOT / "requirements-win-build.lock").read_text(encoding="utf-8")
    for name, required in re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", lock_text, re.M):
        actual = metadata.version(name)
        if actual != required:
            raise RuntimeError(f"Build dependency mismatch: {name} {actual} != {required}")
    commit = git("rev-parse", "HEAD")
    epoch = int(git("show", "-s", "--format=%ct", "HEAD"))
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    tags = set(git("tag", "--points-at", "HEAD").splitlines())
    tag = f"v{version}" if f"v{version}" in tags else None
    info = {
        "version": version,
        "commit": commit,
        "tag": tag,
        "dirty": dirty,
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "pyinstaller": metadata.version("pyinstaller"),
        "lock_sha256": digest(ROOT / "requirements-win-build.lock"),
        "base_lock_sha256": digest(ROOT / "requirements-win.lock"),
        "source_date_epoch": epoch,
    }
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / "work").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="portable-build-", dir=str(ROOT / "work")) as temporary:
        work = Path(temporary)
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(epoch), PYTHONHASHSEED="0", PYINSTALLER_CONFIG_DIR=str(work / "pyinstaller-cache"))
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", str(out_dir), "--workpath", str(work / "build"),
            str(ROOT / "scripts" / "portable.spec"),
        ], cwd=ROOT, env=env, check=True)
    package = out_dir / NAME
    (package / "README.txt").write_text(
        f"微信本地归档 v{version}\n\n"
        "支持 Windows 10/11 x64。解压后直接运行，无需安装 Python 或管理员权限。\n"
        "双击 WeChatLocalArchive.exe 打开图形界面。\n"
        "命令行使用 WeChatLocalArchiveCLI.exe --help；--version 查看版本。\n\n"
        "首次使用：先正常登录 Windows 微信，再在工具中初始化。\n"
        "之后可使用已保存的本地数据库密钥离线导出，无需一直打开微信。\n"
        "首次语音转写需要下载 SenseVoice 模型；本包不包含模型权重。\n"
        "Whisper 二次复核不包含在基础便携包中。\n\n"
        "默认归档：下载\\wechat-local-archive，可在界面修改。\n"
        "加密密钥与设置：%LOCALAPPDATA%\\WeChatLocalArchive。\n"
        "删除程序目录不会删除上述归档或设置。不要随意分享聊天记录和密钥。\n"
        "本工具非微信官方产品，仅提供本地只读归档功能。\n",
        encoding="utf-8-sig",
    )
    (package / "build-info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(ROOT / "LICENSE", package / "LICENSE.txt")
    _copy_licenses(package)
    _remove_build_metadata(package)
    _check_contents(package)
    files = sorted((p for p in package.rglob("*") if p.is_file()), key=lambda p: p.relative_to(package).as_posix())
    (package / "SHA256SUMS.txt").write_text(
        "".join(f"{digest(p)}  {p.relative_to(package).as_posix()}\n" for p in files), encoding="utf-8"
    )
    archive = out_dir / f"{NAME}-v{version}.zip"
    date_time = time.gmtime(max(epoch, 315532800))[:6]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zip_file:
        for path in sorted(package.rglob("*")):
            if not path.is_file():
                continue
            relative = Path(NAME) / path.relative_to(package)
            entry = zipfile.ZipInfo(relative.as_posix(), date_time=date_time)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            with path.open("rb") as stream, zip_file.open(entry, "w", force_zip64=True) as output:
                shutil.copyfileobj(stream, output, 1024 * 1024)
    (out_dir / (archive.name + ".sha256")).write_text(f"{digest(archive)}  {archive.name}\n", encoding="ascii")
    print(f"Portable: {package}")
    print(f"ZIP: {archive}")
    print(f"SHA256: {digest(archive)}")
    return archive


def _remove_build_metadata(package: Path) -> None:
    """PEP 610 metadata can expose the builder's private checkout path."""
    for path in package.rglob("direct_url.json"):
        if path.parent.name.endswith(".dist-info"):
            path.unlink()


def _copy_licenses(package: Path) -> None:
    """Preserve license texts from the pinned dependency distributions."""
    notices = ["Third-party dependency notices", ""]
    lock = (ROOT / "requirements-win.lock").read_text(encoding="utf-8")
    for name in sorted(set(re.findall(r"^([A-Za-z0-9_.-]+)==[^\s]+", lock, re.M)), key=str.casefold):
        distribution = metadata.distribution(name)
        label = distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "See bundled license files"
        if len(label) > 120 or "\n" in label:
            label = "See bundled license files"
        notices.append(f"{distribution.metadata['Name']}=={distribution.version} | {label}")
        copies = 0
        for relative in distribution.files or []:
            filename = Path(str(relative)).name
            if not re.match(r"^(LICENSE|LICENCE|COPYING|NOTICE)(?:[._-]|$)", filename, re.I):
                continue
            source = Path(distribution.locate_file(relative))
            if not source.is_file():
                continue
            target = package / "licenses" / re.sub(r"[^A-Za-z0-9_.-]", "_", name) / f"{copies:03d}-{filename}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copies += 1
    import imageio_ffmpeg

    ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    version_output = subprocess.check_output([str(ffmpeg), "-hide_banner", "-version"], text=True)
    ffmpeg_dir = package / "licenses" / "FFmpeg"
    ffmpeg_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "docs" / "licenses" / "GPL-3.0.txt", ffmpeg_dir / "GPL-3.0.txt")
    (ffmpeg_dir / "SOURCE.txt").write_text(
        "FFmpeg binary distributed by imageio-ffmpeg 0.6.0.\n"
        "License: GPL version 3.\n"
        "Source: https://github.com/FFmpeg/FFmpeg/tree/n7.1\n"
        "Build provider and source information: https://www.gyan.dev/ffmpeg/builds/\n"
        f"Bundled executable SHA256: {digest(ffmpeg)}\n\n"
        + version_output, encoding="utf-8",
    )
    notices.append("FFmpeg 7.1 essentials binary | GPL-3.0 | See licenses/FFmpeg and its source information")
    (package / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(notices) + "\n", encoding="utf-8")


def _check_contents(package: Path) -> None:
    forbidden_names = {"archive.json", "chat.md", "ai.jsonl", "chat.html", "secrets.dpapi", "keys.json", "history.json", "model.bin", "pytorch_model.bin", "direct_url.json"}
    forbidden_parts = {"db_storage", "xwechat_files", "reader-audio"}
    for path in package.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(package)
        if (path.name.casefold() in forbidden_names or
                any(part.casefold() in forbidden_parts for part in relative.parts) or
                path.suffix.casefold() in {".db", ".sqlite", ".sqlite3", ".silk", ".onnx", ".safetensors", ".ckpt"}):
            raise RuntimeError(f"Unexpected private data or model file in package: {relative}")
    for name in ("WeChatLocalArchive.exe", "WeChatLocalArchiveCLI.exe"):
        if not (package / name).is_file():
            raise RuntimeError(f"Missing portable launcher: {name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    build(args.out_dir)
