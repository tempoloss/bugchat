"""Тонкий асинхронный клиент Bot API. Ровно те методы, что нужны боту, без фреймворка."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


class TelegramError(RuntimeError):
    def __init__(self, method: str, code: int, description: str) -> None:
        super().__init__(f"{method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description


@dataclass(frozen=True, slots=True)
class Download:
    data: bytes
    filename: str
    mime: str


class TelegramClient:
    def __init__(self, token: str, *, poll_timeout_s: int) -> None:
        self._token = token
        self._poll_timeout = poll_timeout_s
        self._api = f"https://api.telegram.org/bot{token}"
        self._files = f"https://api.telegram.org/file/bot{token}"
        # read-таймаут обязан пережидать long-poll, иначе каждый опрос рвётся по таймауту.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=poll_timeout_s + 15.0, write=60.0, pool=10.0)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, **payload: Any) -> Any:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await self._client.post(f"{self._api}/{method}", json=payload)
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise
                delay = 2.0 * attempt
                logger.warning("telegram %s: сеть (%s), повтор через %.0fs", method, exc, delay)
                await asyncio.sleep(delay)
                continue

            body = response.json()
            if body.get("ok"):
                return body.get("result")

            code = body.get("error_code", response.status_code)
            description = body.get("description", "")
            retry_after = (body.get("parameters") or {}).get("retry_after")
            if retry_after and attempt < MAX_RETRIES:
                logger.warning("telegram %s: 429, ждём %ss", method, retry_after)
                await asyncio.sleep(float(retry_after) + 0.5)
                continue
            raise TelegramError(method, code, description)

        raise TelegramError(method, 0, "исчерпаны попытки")

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def set_my_commands(self, commands: list[dict[str, str]], *, scope: dict[str, str]) -> None:
        """Меню по «/». Украшение, а не механика: отказ Bot API не должен ронять запуск."""
        try:
            await self._call("setMyCommands", commands=commands, scope=scope)
        except TelegramError as exc:
            logger.warning("меню команд (%s) не обновилось: %s", scope.get("type"), exc.description)

    async def get_updates(self, offset: int) -> list[dict[str, Any]]:
        return await self._call(
            "getUpdates",
            offset=offset,
            timeout=self._poll_timeout,
            limit=50,
            # message_reaction и callback_query не входят в набор по умолчанию —
            # без явного списка ни 👎, ни нажатие кнопки до бота не доедут.
            allowed_updates=["message", "edited_message", "message_reaction", "callback_query"],
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        buttons: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_to is not None:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        return await self._call("sendMessage", **payload)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        buttons: list[dict[str, str]] | None = None,
    ) -> None:
        try:
            await self._call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                link_preview_options={"is_disabled": True},
                # Пустой inline_keyboard снимает кнопку: нажимать её второй раз незачем.
                reply_markup={"inline_keyboard": [buttons] if buttons else []},
            )
        except TelegramError as exc:
            logger.warning("не удалось отредактировать сообщение %s: %s", message_id, exc.description)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Обязательный ответ на нажатие: без него у пользователя крутится часик."""
        try:
            await self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as exc:
            logger.debug("answerCallbackQuery: %s", exc.description)

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        """Реакция — украшение: нет прав, отключены реакции в чате — просто пишем в лог."""
        try:
            await self._call(
                "setMessageReaction",
                chat_id=chat_id,
                message_id=message_id,
                reaction=[{"type": "emoji", "emoji": emoji}],
            )
        except TelegramError as exc:
            logger.debug("реакция %s не поставлена: %s", emoji, exc.description)

    async def get_chat(self, chat_id: int) -> dict[str, Any]:
        return await self._call("getChat", chat_id=chat_id)

    async def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        return await self._call("getChatMember", chat_id=chat_id, user_id=user_id)

    async def download(self, file_id: str, *, max_bytes: int, fallback_name: str, mime: str) -> Download | None:
        try:
            meta = await self._call("getFile", file_id=file_id)
        except TelegramError as exc:
            logger.warning("getFile %s: %s", file_id, exc.description)
            return None

        size = meta.get("file_size") or 0
        if size > max_bytes:
            logger.info("файл %s пропущен: %.1f МБ больше лимита", fallback_name, size / 1024 / 1024)
            return None

        path = meta.get("file_path")
        if not path:
            return None
        try:
            response = await self._client.get(f"{self._files}/{path}")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("скачивание %s не удалось: %s", path, exc)
            return None

        name = fallback_name or path.rsplit("/", 1)[-1]
        return Download(data=response.content, filename=name, mime=mime)
