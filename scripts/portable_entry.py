"""Shared entry point for the windowed and console portable launchers."""

import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    console = Path(sys.executable).stem.casefold() == "wechatlocalarchivecli"
    if console or args:
        from wechat_local_archive.cli import main as cli_main
        return cli_main(args)
    from wechat_local_archive.gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
