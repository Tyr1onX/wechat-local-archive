from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .archive import parse_date
from .exporter import export_chat
from .source import SourceError, bootstrap, discover_accounts, open_offline, resolve_chat
from .state import clear_state, load_config, load_secret


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wechat-archive",
        description="Read-only local WeChat 4.x archive exporter.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

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
    export.add_argument("--batch-size", type=int, default=16)
    export.add_argument("--refresh", action="store_true", help="Rebuild the selected archive instead of incrementally updating it")

    sub.add_parser("reset", help="Remove saved config and DPAPI-protected master key")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "bootstrap":
            return _bootstrap(args)
        if args.command == "chats":
            return _chats(args)
        if args.command == "export":
            return _export(args)
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


def _export(args) -> int:
    start = parse_date(args.start)
    end = parse_date(args.end)
    if start and end and start > end:
        raise ValueError("start date must not be after end date")
    if args.batch_size < 1:
        raise ValueError("batch size must be at least 1")

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
            batch_size=args.batch_size,
            refresh=args.refresh,
            progress=progress,
        )
    print(f"archive: {summary.archive_path}")
    print(f"markdown: {summary.markdown_path}")
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
