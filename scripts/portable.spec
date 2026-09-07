# -*- mode: python ; coding: utf-8 -*-
"""One shared onedir runtime, with windowed GUI and console CLI launchers."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parent
datas = [(str(root / "src" / "wechat_local_archive" / "reader.html"), "wechat_local_archive")]
binaries = []
hiddenimports = [
    "wechat_local_archive.cli",
    "wechat_local_archive.gui",
    "wechat_local_archive.transcribe",
    "wechat_local_archive.quality",
    "wechat_local_archive.html_export",
]

# The optional Whisper dependency is deliberately not installed in the base build.
# Bundle the existing local ASR and image/video decoding runtime, not downloaded models.
for package in ("funasr_onnx", "imageio_ffmpeg"):
    package_data, package_binaries, package_imports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports

# ModelScope loads its hub implementation dynamically. Only the download/runtime
# subtree is needed; do not collect unrelated model families or model caches.
hiddenimports += collect_submodules("modelscope.hub")
for distribution in (
    "wechat-local-archive", "wechatauto-replica", "funasr-onnx",
    "modelscope", "modelscope-hub", "silk-python", "imageio-ffmpeg",
    "onnxruntime", "numpy", "scikit-learn", "librosa",
):
    datas += copy_metadata(distribution)
# Editable/local wheel installations may record the builder's private path.
datas = [(source, target) for source, target in datas if Path(source).name != "direct_url.json"]

analysis = Analysis(
    [str(root / "scripts" / "portable_entry.py")],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["faster_whisper", "ctranslate2", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

def launcher(name, console):
    return EXE(
        pyz, analysis.scripts, [], exclude_binaries=True,
        name=name, debug=False, bootloader_ignore_signals=False,
        strip=False, upx=False, console=console,
        disable_windowed_traceback=False,
    )

gui = launcher("WeChatLocalArchive", False)
cli = launcher("WeChatLocalArchiveCLI", True)
coll = COLLECT(
    gui, cli, analysis.binaries, analysis.datas,
    strip=False, upx=False, name="wechat-local-archive-windows-x64",
)
