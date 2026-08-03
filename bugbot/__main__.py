"""Точка входа: ``py -3 -m bugbot``."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from bugbot.app import BugBot
from bugbot.config import Config


def _setup_logging() -> None:
    if sys.platform == "win32":
        with_reconfigure = getattr(sys.stdout, "reconfigure", None)
        if with_reconfigure is not None:
            with_reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=os.getenv("BUGBOT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%d.%m %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> int:
    _setup_logging()
    config = Config.from_env()
    try:
        asyncio.run(BugBot(config).run())
    except KeyboardInterrupt:
        logging.getLogger("bugbot").info("остановлен по Ctrl+C")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
