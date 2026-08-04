"""Живая проверка связки: синтетическое сообщение → настоящая задача в Plane.

Гоняет реальный путь бота (`BugBot._process`) с заглушкой вместо Telegram, так что
проверяются логин в Plane, метки, создание задачи, вложение и комментарий.
Задача по умолчанию отменяется в конце — с `--keep` останется на доске.

    uv run python scripts/live_check.py [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import dataclasses
import logging
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bugbot.app import BugBot  # noqa: E402
from bugbot.config import Config  # noqa: E402
from bugbot.telegram import Download  # noqa: E402

CHAT_ID = -1009999999999
CHAT = {"id": CHAT_ID, "title": "live_check", "type": "supergroup"}
SENDER = {"id": 1, "first_name": "Live", "last_name": "Check", "username": "live_check", "is_bot": False}

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAIAQMAAAD+wSzIAAAABlBMVEX///+/v7+jQ3Y5AAAADklEQVQI12P4AIX8EAgALgAD/aNpbtEAAAAASUVORK5CYII="
)


class StubTelegram:
    """Тот же интерфейс, что у TelegramClient, но без сети."""

    async def send_message(
        self, chat_id: int, text: str, *, reply_to: int | None = None, buttons: list | None = None
    ) -> dict[str, Any]:
        print(f"  [telegram] ответ в чат: {text}" + (f"  [кнопка: {buttons[0]['text']}]" if buttons else ""))
        return {"message_id": 999_999}

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, *, buttons: list | None = None) -> None:
        print(f"  [telegram] правка ответа: {text}")

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        pass

    async def download(self, file_id: str, *, max_bytes: int, fallback_name: str, mime: str) -> Download:
        return Download(data=PNG, filename=fallback_name, mime=mime)

    async def aclose(self) -> None:
        pass


def message(message_id: int, text: str, *, photo: bool = False, reply_to: dict | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message_id": message_id,
        "chat": CHAT,
        "from": SENDER,
        "date": int(time.time()),
        "caption" if photo else "text": text,
    }
    if photo:
        payload["photo"] = [{"file_id": "stub-file-id", "file_size": len(PNG), "width": 8, "height": 8}]
    if reply_to:
        payload["reply_to_message"] = reply_to
    return payload


async def main(keep: bool) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    with tempfile.TemporaryDirectory() as tmp:
        config = dataclasses.replace(
            Config.from_env(), db_path=Path(tmp) / "live_check.db", chat_ids=frozenset({CHAT_ID})
        )
        bot = BugBot(config)
        bot._tg = StubTelegram()  # type: ignore[assignment]  # проверяем Plane, не Telegram
        await bot._plane.bootstrap()

        try:
            print("\n1) сообщение-жалоба со скриншотом → задача")
            source = message(1, "live_check: не открывается карточка клиента, 500 на бэке", photo=True)
            await bot._process([source])
            ref = bot._store.issue_for_message(CHAT_ID, 1)
            if ref is None:
                print("ПРОВАЛ: задача не создана")
                return 1
            print(f"   → {ref.url}")

            print("\n2) ответ в ветке → комментарий")
            await bot._process([message(2, "воспроизводится и на стейдже", reply_to=source)])

            print("\n3) болтовня → игнор")
            await bot._process([message(3, "ок, спасибо")])
            if bot._store.issue_for_message(CHAT_ID, 3) is not None:
                print("ПРОВАЛ: болтовня попала в Plane")
                return 1

            print("\n4) заявка на функционал → задача с меткой feature и приоритетом low")
            await bot._process([message(4, "live_check: добавьте выгрузку реестра в excel")])
            wish = bot._store.issue_for_message(CHAT_ID, 4)
            if wish is None:
                print("ПРОВАЛ: заявка не создана")
                return 1

            # Метку и приоритет читаем из Plane, а не из своих же намерений: label_ids
            # эта сборка умеет глотать на create, и проверка по локальной переменной
            # прошла бы при пустых метках на доске.
            issue = await bot._plane.get_issue(wish.issue_id)
            labels = await bot._plane.label_names(issue.get("label_ids") or [])
            print(f"   → {wish.url}  метки: {', '.join(labels) or 'НЕТ'}  приоритет: {issue.get('priority')}")
            if config.plane_feature_label.lower() not in {name.lower() for name in labels}:
                print(f"ПРОВАЛ: нет метки {config.plane_feature_label}")
                return 1
            if config.plane_bug_label.lower() in {name.lower() for name in labels}:
                print(f"ПРОВАЛ: заявка помечена как {config.plane_bug_label}")
                return 1
            if issue.get("priority") != "low":
                print(f"ПРОВАЛ: приоритет {issue.get('priority')}, ожидался low")
                return 1

            if keep:
                print(f"\nготово, задачи остались: {ref.url} и {wish.url}")
            else:
                print("\n5) отмена задач")
                await bot._cancel(ref, reason="live_check")
                await bot._cancel(wish, reason="live_check")
                print(f"   отменены: {ref.url}, {wish.url}")
        finally:
            await bot.aclose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="не отменять созданную задачу")
    raise SystemExit(asyncio.run(main(parser.parse_args().keep)))
