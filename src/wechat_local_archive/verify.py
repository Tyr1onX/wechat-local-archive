from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .archive import Archive, archive_from_dict, selected_media_type
from .exporter import render_markdown


@dataclass(frozen=True, slots=True)
class VerifyResult:
    root: Path
    message_count: int
    attachment_count: int
    voice_count: int
    voice_transcript_count: int
    orphan_count: int
    pruned_count: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def verify_archive(root: Path, prune_orphans: bool = False) -> VerifyResult:
    root = root.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    archive: Archive | None = None

    if not root.is_dir():
        errors.append(f"archive directory does not exist: {root}")
        return _result(root, None, set(), 0, 0, errors, warnings)

    archive_path = root / "archive.json"
    try:
        raw = json.loads(archive_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("archive root must be an object")
        archive = archive_from_dict(raw)
    except FileNotFoundError:
        errors.append("archive.json is missing")
        return _result(root, None, set(), 0, 0, errors, warnings)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"archive.json is invalid: {exc}")
        return _result(root, None, set(), 0, 0, errors, warnings)

    if not archive.account:
        errors.append("archive account metadata is missing")
    if not archive.conversation_id:
        errors.append("archive conversation id is missing")
    if not archive.conversation_name:
        errors.append("archive conversation name is missing")

    seen_ids: set[str] = set()
    previous_time: int | None = None
    referenced_assets: set[Path] = set()
    attachment_count = 0
    voice_count = 0
    voice_transcript_count = 0

    for index, message in enumerate(archive.messages):
        if not message.id:
            errors.append(f"message #{index + 1} has an empty id")
        elif message.id in seen_ids:
            errors.append(f"duplicate message id: {message.id}")
        else:
            seen_ids.add(message.id)

        if previous_time is not None and message.timestamp_unix < previous_time:
            errors.append(
                f"messages are out of chronological order at {message.id or '#' + str(index + 1)}"
            )
        previous_time = message.timestamp_unix

        if selected_media_type(message.type_code) == 34:
            voice_count += 1
            if message.transcript:
                voice_transcript_count += 1

        if not message.attachment:
            continue
        attachment_count += 1
        candidate = Path(message.attachment)
        candidate = candidate if candidate.is_absolute() else root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            errors.append(f"attachment escapes archive root for message {message.id}: {message.attachment}")
            continue
        referenced_assets.add(resolved)
        if not resolved.is_file():
            errors.append(f"attachment is missing for message {message.id}: {message.attachment}")

    markdown_path = root / "chat.md"
    if not markdown_path.is_file():
        errors.append("chat.md is missing")
    else:
        try:
            actual_markdown = markdown_path.read_text(encoding="utf-8")
            expected_markdown = render_markdown(archive)
            if actual_markdown != expected_markdown:
                errors.append("chat.md is stale or does not match archive.json")
        except OSError as exc:
            errors.append(f"chat.md is unreadable: {exc}")

    orphan_paths = _find_orphans(root, referenced_assets)
    pruned_count = 0
    if prune_orphans:
        if errors:
            warnings.append("orphan pruning skipped because archive verification failed")
        else:
            for path in orphan_paths:
                path.unlink()
                pruned_count += 1
            _remove_empty_asset_dirs(root / "assets")
            orphan_paths = []

    return VerifyResult(
        root=root,
        message_count=len(archive.messages),
        attachment_count=attachment_count,
        voice_count=voice_count,
        voice_transcript_count=voice_transcript_count,
        orphan_count=len(orphan_paths),
        pruned_count=pruned_count,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _find_orphans(root: Path, referenced_assets: set[Path]) -> list[Path]:
    assets_root = root / "assets"
    if not assets_root.is_dir():
        return []
    return sorted(
        path
        for path in assets_root.rglob("*")
        if path.is_file() and path.resolve() not in referenced_assets
    )


def _remove_empty_asset_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    directories = sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True)
    for path in directories:
        try:
            path.rmdir()
        except OSError:
            pass


def _result(
    root: Path,
    archive: Archive | None,
    referenced_assets: set[Path],
    orphan_count: int,
    pruned_count: int,
    errors: list[str],
    warnings: list[str],
) -> VerifyResult:
    return VerifyResult(
        root=root,
        message_count=len(archive.messages) if archive else 0,
        attachment_count=len(referenced_assets),
        voice_count=0,
        voice_transcript_count=0,
        orphan_count=orphan_count,
        pruned_count=pruned_count,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
