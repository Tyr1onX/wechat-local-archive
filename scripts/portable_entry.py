"""Shared entry point for the browser UI and console portable launchers."""

import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    console = Path(sys.executable).stem.casefold() == "wechatlocalarchivecli"
    if console or args:
        from wechat_local_archive.cli import main as cli_main
        return cli_main(args)
    from wechat_local_archive.web import main as web_main
    return web_main()


if __name__ == "__main__":
    raise SystemExit(main())
