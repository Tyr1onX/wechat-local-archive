from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .ai_export import rebuild_ai_jsonl
from .html_export import rebuild_html
from .archive import parse_date
from .exporter import export_chat
from .source import SourceError, bootstrap, discover_accounts, open_offline, resolve_chat
from .state import clear_state, load_config, load_secret
from .transcribe import resolve_asr_settings
from .verify import verify_archive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wechat-archive",
        description="Read-only local WeChat 4.x archive exporter.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gui", help="Open the minimal desktop archive window")

    doctor = sub.add_parser("doctor", help="Check local data discovery and offline bootstrap state")
    doctor.add_argument("--db-dir", default=None)

    boot = sub.add_parser("bootstrap", help="Capture the database master key once from an already logged-in WeChat")
    boot.add_argument("--db-dir", default=None)
    boot.add_argument("--account", default=None)

    chats = sub.add_parser("chats", help="List chats using only local database files")
    chats.add_argument("--search", default="")
    chats.add_argument("--limit", type=int, default=50)

    export = sub.add_parser("export", help="Export one chat to archive.json and chat.md")
    export.add_argument("chat", help="Conversation username, exact display name, or unique substring")
    export.add_argument("--out", required=True, type=Path)
    export.add_argument("--start", default=None, help="YYYY-MM-DD")
    export.add_argument("--end", default=None, help="YYYY-MM-DD")
    export.add_argument("--media", choices=("none", "voice", "all"), default="none")
    export.add_argument("--transcribe", action="store_true", help="Batch-transcribe extracted voice with SenseVoiceSmall")
    export.add_argument("--asr-preset", choices=("background", "balanced", "fast"), default="balanced")
    export.add_argument("--batch-size", type=int, default=None, help="Advanced batch-size override for the selected ASR preset")
    export.add_argument("--refresh", action="store_true", help="Rebuild the selected archive instead of incrementally updating it")

    ai = sub.add_parser("ai", help="Rebuild ai.jsonl from an existing archive without opening WeChat")
    ai.add_argument("archive_dir", type=Path)

    reader = sub.add_parser("html", help="Rebuild the offline chat.html reader without opening WeChat")
    reader.add_argument("archive_dir", type=Path)
    reader.add_argument("--no-audio", action="store_true", help="Skip SILK conversion; keep transcripts and original audio links")

    verify = sub.add_parser("verify", help="Verify one archive directory and its attachment references")
    verify.add_argument("archive_dir", type=Path)
    verify.add_argument("--prune-orphans", action="store_true", help="Delete unreferenced files under assets/ after a successful verification")

    sub.add_parser("reset", help="Remove saved config and DPAPI-protected master key")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "gui":
            from .gui import main as gui_main
            return gui_main()
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "bootstrap":
            return _bootstrap(args)
        if args.command == "chats":
            return _chats(args)
        if args.command == "export":
            return _export(args)
        if args.command == "ai":
            print(f"ai: {rebuild_ai_jsonl(args.archive_dir)}")
            return 0
        if args.command == "html":
            path, warnings = rebuild_html(args.archive_dir, convert_audio=not args.no_audio)
            print(f"html: {path}")
            for warning in warnings:
                print(f"warning: {warning}")
            return 0
        if args.command == "verify":
            return _verify(args)
        if args.command == "reset":
            clear_state()
            print("Local bootstrap state removed.")
            return 0
    except (SourceError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


def _doctor(args) -> int:
    config = load_config()
    secret = load_secret()
    accounts = discover_accounts(args.db_dir or (config.db_dir if config else None))
    print(f"local accounts: {len(accounts)}")
    for item in accounts[:10]:
        print(f"  {item.get('account')}  {item.get('path')}")
    if config:
        print(f"configured account: {config.account}")
        print(f"configured data root: {config.db_dir}")
    else:
        print("configured account: none")
    print(f"DPAPI database keys: {'ready' if secret else 'missing'}")
    if config and secret:
        account_dir = Path(config.db_dir) / config.account / "db_storage"
        print(f"offline mode: {'ready' if account_dir.is_dir() else 'data directory missing'}")
    else:
        print("offline mode: bootstrap required")
    return 0


def _bootstrap(args) -> int:
    print("Reading the already logged-in WeChat process once; no login automation or restart is performed.")
    config = bootstrap(db_dir=args.db_dir, account=args.account)
    print(f"bootstrap complete: {config.account}")
    print("database keys saved with Windows DPAPI; WeChat may now be closed for later exports.")
    return 0


def _chats(args) -> int:
    with open_offline() as session:
        chats = session.list_chats()
    needle = args.search.strip().casefold()
    if needle:
        chats = [
            item
            for item in chats
            if needle in str(item.get("name", "")).casefold()
            or needle in str(item.get("username", "")).casefold()
        ]
    for item in chats[: max(0, args.limit)]:
        print(f"{item.get('message_count', 0):>7}  {item.get('name', '')}  [{item.get('username', '')}]")
    return 0


def _verify(args) -> int:
    result = verify_archive(args.archive_dir, prune_orphans=args.prune_orphans)
    print(f"archive: {result.root}")
    print(f"status: {'PASS' if result.ok else 'FAIL'}")
    print(f"messages: {result.message_count}")
    print(f"attachments: {result.attachment_count}")
    print(f"voice transcripts: {result.voice_transcript_count}/{result.voice_count}")
    print(f"orphans: {result.orphan_count}")
    if result.pruned_count:
        print(f"pruned: {result.pruned_count}")
    for warning in result.warnings:
        print(f"warning: {warning}")
    for error in result.errors:
        print(f"error: {error}", file=sys.stderr)
    return 0 if result.ok else 2


def _export(args) -> int:
    start = parse_date(args.start)
    end = parse_date(args.end)
    if start and end and start > end:
        raise ValueError("start date must not be after end date")
    settings = resolve_asr_settings(args.asr_preset, args.batch_size)
    if args.transcribe:
        print(
            f"ASR preset: {settings.preset} "
            f"(batch={settings.batch_size}, threads={settings.threads})"
        )

    def progress(current: int, total: int, label: str) -> None:
        if total > 0:
            print(f"[{current}/{total}] {label}")
        else:
            print(label)

    with open_offline() as session:
        chat = resolve_chat(session.list_chats(), args.chat)
        summary = export_chat(
            session,
            conversation_id=str(chat["username"]),
            conversation_name=str(chat.get("name") or chat["username"]),
            output_dir=args.out,
            start=start,
            end=end,
            media=args.media,
            transcribe=args.transcribe,
            asr_preset=args.asr_preset,
            batch_size=args.batch_size,
            refresh=args.refresh,
            progress=progress,
        )
    print(f"archive: {summary.archive_path}")
    print(f"markdown: {summary.markdown_path}")
    print(f"ai: {summary.archive_path.with_name('ai.jsonl')}")
    print(f"messages: {summary.message_count}")
    print(f"attachments: {summary.attachment_count}")
    print(f"transcripts: {summary.transcript_count}")
    if summary.warnings:
        print(f"warnings: {len(summary.warnings)}")
        for warning in summary.warnings[:20]:
            print(f"  - {warning}")
        if len(summary.warnings) > 20:
            print(f"  ... {len(summary.warnings) - 20} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
