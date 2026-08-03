"""Цикл бота: long-poll Telegram → триаж → задача в Plane."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import html
import logging
from typing import Any

import httpx

from bugbot import plane, plane_html, triage
from bugbot.config import Config
from bugbot.plane import IssueState, PlaneClient, PlaneError
from bugbot.store import IssueRef, Store
from bugbot.telegram import Download, TelegramClient, TelegramError

logger = logging.getLogger("bugbot")

CANCEL_EMOJI = frozenset({"👎", "💩"})
SEEN_EMOJI = "👀"
PRIORITY_LABEL = {"urgent": "🔥 urgent", "high": "high", "medium": "medium", "low": "low", "none": "без приоритета"}
VAULT_COMMANDS = frozenset({"/my", "/мои", "/tasks", "/задачи"})
TASK_COMMANDS = frozenset({"/task", "/задача"})
COMMANDS = frozenset({"/chatid", "/ping", "/skip", "/help", "/start"}) | VAULT_COMMANDS | TASK_COMMANDS
CANCEL_PREFIX = "skip:"
MAX_TASK_SPAN = 20
"""Потолок для `/task N`: больше двадцати сообщений одной задачей — почти наверняка опечатка."""
STATE_SECTIONS = (
    ("started", "🔧 В работе"),
    ("unstarted", "🕗 Взяли в план"),
    ("backlog", "📥 Ждут разбора"),
    ("completed", "✅ Готово"),
    ("cancelled", "🚫 Отклонено"),
)

# Меню по «/» в клиенте. Bot API принимает только латиницу, поэтому русских
# алиасов тут нет — набранные руками они работают по-прежнему.
GROUP_MENU = (
    ("skip", "отменить задачу: ответом на сообщение или последнюю в чате"),
    ("my", "мои задачи и их статусы"),
    ("help", "что я умею"),
    ("ping", "жив ли я и куда пишу задачи"),
    ("chatid", "id этого чата — для настройки"),
)
ADMIN_MENU = (("task", "ответом на сообщение — завести задачу; /task 4 — четыре сообщения одной"), *GROUP_MENU)
"""`/task` обходит триаж, поэтому и в меню он только у админов чата."""
PRIVATE_MENU = (
    ("my", "мои задачи и их статусы"),
    ("help", "что я умею"),
    ("ping", "жив ли я и куда пишу задачи"),
)
GROUP_HELP = (
    "Ловлю баг-репорты в этом чате и завожу задачи в Plane.\n"
    "\n"
    "• сообщение с жалобой или скриншотом → новая задача\n"
    "• несколько сообщений подряд про одно → склею в одну\n"
    "• ответ на такое сообщение → комментарий к задаче\n"
    "• кнопка «🚫 Не баг» или <code>/skip</code> → задача отменяется\n"
    "\n"
    "Если не распознал сам (админы чата):\n"
    "• <code>/task</code> ответом → завести принудительно\n"
    "• <code>/task 4</code> ответом на первое из четырёх → одна задача из них\n"
    "\n"
    "<code>/my</code> — мои задачи и статусы, в личке тоже работает.\n"
    "<code>/ping</code>, <code>/chatid</code> — служебные."
)


def cancel_button(issue_id: str) -> dict[str, str]:
    """Кнопка под ответом бота. Одно нажатие вместо ответа командой:
    тап по `/skip` в тексте отправляет команду обычным сообщением, без reply,
    и бот не понимает, какую задачу гасить."""
    return {"text": "🚫 Не баг", "callback_data": f"{CANCEL_PREFIX}{issue_id}"}


@dataclass(frozen=True, slots=True)
class MediaRef:
    file_id: str
    filename: str
    mime: str
    width: int = 0
    height: int = 0

    @property
    def is_image(self) -> bool:
        """Картинки уходят в тело описания, остальное — во вложения."""
        return self.mime.startswith("image/")


def media_of(message: dict[str, Any]) -> MediaRef | None:
    """Вложение сообщения. Стикеры сознательно не считаем: это реакция, а не баг-репорт."""
    message_id = message.get("message_id", 0)

    if photo := message.get("photo"):
        best = max(photo, key=lambda size: size.get("file_size") or 0)
        return MediaRef(
            best["file_id"],
            f"screenshot_{message_id}.jpg",
            "image/jpeg",
            width=best.get("width") or 0,
            height=best.get("height") or 0,
        )
    if document := message.get("document"):
        return MediaRef(
            document["file_id"],
            document.get("file_name") or f"file_{message_id}",
            document.get("mime_type") or "application/octet-stream",
        )
    if video := message.get("video"):
        return MediaRef(video["file_id"], video.get("file_name") or f"video_{message_id}.mp4", "video/mp4")
    if animation := message.get("animation"):
        # screen recording с десктопа часто приезжает именно как animation
        return MediaRef(animation["file_id"], animation.get("file_name") or f"clip_{message_id}.mp4", "video/mp4")
    if voice := message.get("voice"):
        return MediaRef(voice["file_id"], f"voice_{message_id}.ogg", voice.get("mime_type") or "audio/ogg")
    if audio := message.get("audio"):
        return MediaRef(audio["file_id"], audio.get("file_name") or f"audio_{message_id}", "audio/mpeg")
    return None


def _task_count(text: str) -> int:
    """`/task 4` → 4. Без числа — одно сообщение; мусор после команды игнорируем."""
    parts = text.split()
    if len(parts) > 1 and parts[1].isdigit():
        return max(1, min(int(parts[1]), MAX_TASK_SPAN))
    return 1


def text_of(message: dict[str, Any]) -> str:
    return (message.get("text") or message.get("caption") or "").strip()


class BugBot:
    def __init__(self, config: Config) -> None:
        self._cfg = config
        self._store = Store(config.db_path)
        self._tg = TelegramClient(config.bot_token, poll_timeout_s=config.poll_timeout_s)
        self._plane = PlaneClient(config)
        self._me: dict[str, Any] = {}
        self._unknown_chats: set[int] = set()
        self._extra_chats: set[int] = set()
        """Id, доехавшие в рантайме (апгрейд группы в супергруппу). До перезапуска."""
        self._chat_titles: dict[int, str] = {}
        self._dm_seen: set[int] = set()
        """Кому в личке уже показали полную справку — второй раз она незачем."""

    # ---- жизненный цикл --------------------------------------------------
    async def run(self) -> None:
        self._me = await self._tg.get_me()
        logger.info("telegram: @%s (id %s)", self._me.get("username"), self._me.get("id"))
        await self._plane.bootstrap()
        await self._inspect_chats()
        await self._publish_commands()

        background = [
            asyncio.create_task(self._album_loop()),
            asyncio.create_task(self._burst_loop()),
            asyncio.create_task(self._closure_loop()),
        ]
        try:
            while True:
                await self._tick()
        finally:
            for task in background:
                task.cancel()
            for task in background:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self.aclose()

    async def aclose(self) -> None:
        await self._tg.aclose()
        await self._plane.aclose()
        self._store.close()

    async def _inspect_chats(self) -> None:
        if not self._cfg.chat_ids:
            logger.warning("BUGBOT_CHAT_IDS пуст — бот отвечает только на /chatid и /ping. Напишите /chatid в чате.")
            return

        reads_all = bool(self._me.get("can_read_all_group_messages"))
        for chat_id in sorted(self._cfg.chat_ids):
            try:
                chat = await self._tg.get_chat(chat_id)
                member = await self._tg.get_chat_member(chat_id, int(self._me["id"]))
            except TelegramError as exc:
                logger.error("чат %s недоступен: %s", chat_id, exc.description)
                continue

            status = member.get("status")
            title = chat.get("title") or ""
            if title:
                self._chat_titles[chat_id] = title
            logger.info("слушаем %s «%s» (роль бота: %s)", chat_id, title or chat_id, status)
            if status not in ("administrator", "creator") and not reads_all:
                logger.warning(
                    "бот НЕ админ в %s, а privacy mode включён → обычные сообщения не придут. "
                    "Сделайте бота админом чата (или /setprivacy → Disable у @BotFather).",
                    chat_id,
                )

    async def _publish_commands(self) -> None:
        """Список команд, который Telegram показывает по «/». Не критично для работы:
        если Bot API откажет, бот просто останется без подсказки в клиенте."""
        menus = (
            ("all_group_chats", GROUP_MENU),
            # Перекрывает all_group_chats для админов — им видно и `/task`.
            ("all_chat_administrators", ADMIN_MENU),
            ("all_private_chats", PRIVATE_MENU),
        )
        for scope, menu in menus:
            await self._tg.set_my_commands(
                [{"command": name, "description": text} for name, text in menu],
                scope={"type": scope},
            )

    # ---- опрос -----------------------------------------------------------
    async def _tick(self) -> None:
        try:
            updates = await self._tg.get_updates(self._store.offset)
        except (TelegramError, httpx.HTTPError) as exc:
            logger.error("getUpdates: %s — повтор через 5с", exc)
            await asyncio.sleep(5)
            return

        for update in updates:
            update_id = update.get("update_id", 0)
            # Offset двигаем до обработки: «ядовитый» апдейт не должен зациклить бота.
            self._store.offset = update_id + 1
            if self._store.is_processed(update_id):
                continue
            try:
                await self._dispatch(update)
            except Exception as exc:  # noqa: BLE001 — падать целиком из-за одного сообщения нельзя
                logger.exception("апдейт %s не обработан", update_id)
                self._store.dead_letter(update_id, update, repr(exc))
            self._store.mark_processed(update_id)

    async def _album_loop(self) -> None:
        """Альбом приезжает несколькими апдейтами; собрав его, отдаём в буфер серии —
        подпись к скринам часто досылают отдельным сообщением следом."""
        while True:
            await asyncio.sleep(1.0)
            for group in self._store.due_albums(self._cfg.album_debounce_s):
                parts = self._store.pop_album(group)
                if not parts:
                    continue
                anchor = parts[0]
                chat_id = anchor["chat"]["id"]
                user_id = int((anchor.get("from") or {}).get("id") or 0)
                for part in parts:
                    self._store.add_burst_part(chat_id, user_id, part["message_id"], part)

    async def _burst_loop(self) -> None:
        """Серия сообщений одного автора = одна задача. Ждём тишины от него."""
        ticks = 0
        while True:
            await asyncio.sleep(1.0)
            ticks += 1
            if ticks % 300 == 0:
                dropped = self._store.prune_messages(self._cfg.message_log_days * 86400)
                if dropped:
                    logger.debug("журнал сообщений: удалено %d старых", dropped)

            for chat_id, user_id in self._store.due_bursts(self._cfg.burst_quiet_s):
                parts = self._store.pop_burst(chat_id, user_id)
                if not parts:
                    continue
                try:
                    await self._process(parts)
                except Exception as exc:  # noqa: BLE001
                    # Части уже вынуты из буфера: без dead-letter серия пропала бы бесследно.
                    logger.exception("серия из чата %s не обработана", chat_id)
                    self._store.dead_letter(None, {"chat_id": chat_id, "parts": parts}, repr(exc))

    async def _closure_loop(self) -> None:
        """Следит за Plane и отвечает в чат, когда задачу закрыли."""
        if self._cfg.close_poll_s <= 0:
            logger.info("слежение за закрытием выключено (BUGBOT_CLOSE_POLL_SECONDS=0)")
            return
        while True:
            await asyncio.sleep(self._cfg.close_poll_s)
            try:
                await self._check_closures()
            except (PlaneError, httpx.HTTPError) as exc:
                logger.warning("опрос Plane не удался: %s", exc)
            except Exception:  # noqa: BLE001
                logger.exception("опрос Plane упал")

    async def _check_closures(self) -> None:
        tracked = self._store.tracked_issues()
        if not tracked:
            return
        states = await self._plane.issue_states()

        for ref in tracked:
            state = states.get(ref.issue_id)
            if state is None or state.group == ref.last_state_group:
                continue  # задачу удалили из Plane либо состояние не менялось

            # Пишем новое состояние ДО отправки: уведомление в худшем случае теряется,
            # но чат не получит одно и то же каждую минуту, если Telegram упрётся.
            self._store.set_state_group(ref.issue_id, state.group)

            # Чат убрали из BUGBOT_CHAT_IDS — писать туда мы больше не вправе.
            # Состояние выше уже записано: вернут чат в список — не хлынет пачка
            # уведомлений про всё, что закрылось за это время.
            if not self._allowed(ref.chat_id):
                continue

            if ref.last_state_group is None:
                continue  # строка из старой версии базы — просто запоминаем текущее
            if state.group not in ("completed", "cancelled"):
                continue
            if state.group == "cancelled" and ref.cancelled:
                continue  # отменили из чата по /skip — там уже всё написано
            await self._notify_closed(ref, state)

    async def _notify_closed(self, ref: IssueRef, state: IssueState) -> None:
        detail = await self._plane.get_issue(ref.issue_id)
        key = f"{self._plane.identifier}-{ref.sequence_id}"
        done = state.group == "completed"

        lines = [
            f'{"✅ Готово" if done else "🚫 Отклонено"} — <a href="{html.escape(ref.url)}">{key}</a>',
            f"<b>{html.escape(detail.get('name') or state.name)}</b>",
        ]
        if resolution := await self._resolution_of(ref, detail):
            lines += ["", f"<b>Что сделали:</b> {resolution}"]
        elif done:
            lines += ["", "<i>Комментариев не оставили.</i>"]
        lines += [
            "",
            f"<i>{'Закрыл' if done else 'Отклонил'}: {html.escape(self._plane.member_name(state.updated_by))}</i>",
        ]

        await self._send_html_or_plain(ref.chat_id, "\n".join(lines), reply_to=ref.message_id)
        logger.info("в чат отправлено закрытие %s (%s)", key, state.group)

    async def _resolution_of(self, ref: IssueRef, detail: dict[str, Any]) -> str:
        """Что именно написали по задаче: правка описания или последний живой комментарий.

        Исходную жалобу не пересказываем — она в том же треде, прямо над ответом.
        Свои же комментарии (пересланные из чата) тоже отбрасываем, иначе бот
        зациклится на пересказе самого себя.
        """
        # limit с запасом: сверху шапка, снизу подпись, а Telegram режет сообщение на 4096.
        described = plane_html.to_telegram_html(detail.get("description_html") or "", limit=3000)
        if added := plane_html.strip_known_prefix(described, ref.source_text):
            return added

        try:
            comments = await self._plane.list_comments(ref.issue_id)
        except PlaneError as exc:
            logger.warning("комментарии задачи #%s не получены: %s", ref.sequence_id, exc)
            return ""
        ours = self._store.bot_comment_ids(ref.issue_id)
        for comment in reversed(comments):
            if comment.get("id") in ours:
                continue
            body = plane_html.to_telegram_html(comment.get("comment_html") or "", limit=3000)
            if body:
                who = self._plane.member_name(comment.get("actor") or comment.get("created_by"))
                return f"{body}\n<i>— {html.escape(who)}</i>"
        return ""

    async def _send_html_or_plain(self, chat_id: int, text: str, *, reply_to: int) -> None:
        """Описание из Plane пишут люди: если разметка окажется недопустимой для
        Bot API, шлём тем же ответом плоский текст, а не теряем уведомление."""
        try:
            await self._tg.send_message(chat_id, text, reply_to=reply_to)
        except TelegramError as exc:
            if "parse" not in exc.description.lower():
                raise
            logger.warning("Telegram не принял разметку (%s) — шлём без неё", exc.description)
            await self._tg.send_message(chat_id, plane_html.to_plain_text(text), reply_to=reply_to)

    async def _dispatch(self, update: dict[str, Any]) -> None:
        if message := update.get("message"):
            await self._on_message(message, edited=False)
        elif edited := update.get("edited_message"):
            await self._on_message(edited, edited=True)
        elif reaction := update.get("message_reaction"):
            await self._on_reaction(reaction)
        elif callback := update.get("callback_query"):
            await self._on_callback(callback)

    async def _on_callback(self, callback: dict[str, Any]) -> None:
        """Нажатие кнопки «Не баг» под ответом бота."""
        data = callback.get("data") or ""
        callback_id = callback["id"]
        if not data.startswith(CANCEL_PREFIX):
            await self._tg.answer_callback(callback_id)
            return

        ref = self._store.issue_by_id(data[len(CANCEL_PREFIX) :])
        if ref is None:
            await self._tg.answer_callback(callback_id, "Задача не найдена")
            return
        if ref.cancelled:
            await self._tg.answer_callback(callback_id, "Уже отменена")
            return

        await self._cancel(ref, reason=f"кнопка, {triage.author_name(callback.get('from'))}")
        await self._tg.answer_callback(callback_id, "Отменил, задача закрыта")

    # ---- сообщения -------------------------------------------------------
    async def _on_message(self, message: dict[str, Any], *, edited: bool) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or (message.get("from") or {}).get("is_bot"):
            return

        text = text_of(message)
        if command := self._command_of(text):
            await self._on_command(command, message)
            return

        # В личку человек пришёл к боту, а не в чат: молчание тут выглядит поломкой.
        if chat.get("type") == "private":
            if not edited:
                await self._answer_private(message)
            return

        if not self._allowed(chat_id):
            self._note_unknown_chat(chat_id, chat)
            return

        # Обычная группа при апгрейде в супергруппу МЕНЯЕТ chat_id: без этого бот
        # молча оглох бы до правки .env. Слушаем новый id сразу, а в лог — что поправить.
        if migrated := message.get("migrate_to_chat_id"):
            self._extra_chats.add(int(migrated))
            logger.warning(
                "чат %s стал супергруппой %s — слушаю новый id до перезапуска, впишите его в BUGBOT_CHAT_IDS",
                chat_id,
                migrated,
            )
            return

        # Журнал сообщений: Bot API не отдаёт сообщение по id, а `/task 4` должен
        # дотянуться до соседних. Команды не пишем — это не содержание.
        user_id = int((message.get("from") or {}).get("id") or 0)
        self._store.log_message(chat_id, message["message_id"], user_id, message)

        if edited:
            await self._on_edit(message)
            return

        # Ответ в ветке заведённой задачи — комментарий сразу, ждать серию незачем.
        parent = message.get("reply_to_message")
        if parent and self._ref_for(chat_id, parent["message_id"]) is not None:
            await self._process([message])
            return

        if group := message.get("media_group_id"):
            self._store.add_album_part(group, chat_id, message["message_id"], message)
            return

        # Всё остальное — в буфер серии: один баг часто расписывают несколькими
        # сообщениями подряд, и триажить их надо вместе, а не по одному.
        self._store.add_burst_part(chat_id, user_id, message["message_id"], message)

    def _command_of(self, text: str) -> str | None:
        if not text.startswith("/"):
            return None
        token = text.split(maxsplit=1)[0].lower()
        token = token.split("@", 1)[0]
        return token if token in COMMANDS else None

    async def _on_command(self, command: str, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat["id"]
        message_id = message["message_id"]

        if command == "/chatid":
            known = "уже в списке" if self._allowed(chat_id) else "НЕ в списке — впишите в BUGBOT_CHAT_IDS"
            await self._tg.send_message(
                chat_id,
                f"chat_id: <code>{chat_id}</code>\nтип: {chat.get('type')}\n{known}",
                reply_to=message_id,
            )
            logger.info("/chatid → %s (%s)", chat_id, chat.get("title") or chat.get("type"))
            return

        if command == "/ping":
            await self._tg.send_message(
                chat_id,
                f"живой. проект <b>{self._cfg.plane_project}</b>, новые задачи → {self._cfg.plane_state}",
                reply_to=message_id,
            )
            return

        if command in ("/help", "/start"):
            private = chat.get("type") == "private"
            if private:
                self._dm_seen.add(chat_id)
            await self._tg.send_message(
                chat_id,
                self._private_help() if private else GROUP_HELP,
                reply_to=message_id,
            )
            return

        if command in VAULT_COMMANDS:
            await self._show_vault(message)
            return

        # Дальше — команды, которым нужен рабочий чат. В личке отвечаем, что не тут,
        # в чужой группе молчим: бота туда никто не звал работать.
        if command in TASK_COMMANDS or command == "/skip":
            if chat.get("type") == "private":
                await self._tg.send_message(
                    chat_id,
                    f"<code>{command}</code> работает в рабочем чате — там, где лежит сообщение с багом.\n"
                    "Здесь могу показать твои задачи: <code>/my</code>",
                    reply_to=message_id,
                )
                return
            if not self._allowed(chat_id):
                self._note_unknown_chat(chat_id, chat)
                return

        if command in TASK_COMMANDS:
            await self._force_task(message)
            return

        if command == "/skip":
            parent = message.get("reply_to_message")
            if parent:
                ref = self._ref_for(chat_id, parent["message_id"])
            else:
                # Тап по «/skip» в тексте шлёт команду без reply — гасим последнюю
                # заведённую в этом чате задачу, а не отвечаем «сделайте reply».
                ref = self._store.last_open_issue(chat_id)
            if ref is None:
                await self._tg.send_message(chat_id, "нечего отменять: задачи по этому чату нет", reply_to=message_id)
                return
            if ref.cancelled:
                await self._tg.send_message(chat_id, "задача уже отменена", reply_to=message_id)
                return
            await self._cancel(ref, reason="/skip")

    # ---- принудительное заведение ----------------------------------------
    async def _force_task(self, message: dict[str, Any]) -> None:
        """`/task [N]` ответом: завести задачу из сообщения (и N-1 следующих) без триажа."""
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]
        user_id = int((message.get("from") or {}).get("id") or 0)

        if not await self._is_chat_admin(chat_id, user_id):
            await self._tg.send_message(
                chat_id,
                "<code>/task</code> — для админов чата: команда обходит фильтр, "
                "и ей легко засыпать доску.\nПросто напиши про баг сообщением — разберу сам.",
                reply_to=message_id,
            )
            logger.info("/task от не-админа %s в %s", user_id, chat_id)
            return

        parent = message.get("reply_to_message")
        if not parent:
            await self._tg.send_message(
                chat_id,
                "ответьте <code>/task</code> на сообщение, которое надо завести задачей.\n"
                "<code>/task 4</code> — это сообщение и три следующих одной задачей.",
                reply_to=message_id,
            )
            return

        count = _task_count(text_of(message))
        known = self._store.messages_from(chat_id, parent["message_id"], count)
        # Журнал мог не застать сообщение (бота добавили позже) — тогда работаем с реплаем.
        picked = known or [parent]
        picked = [item for item in picked if not text_of(item).startswith("/")][:count]

        fresh = [item for item in picked if self._store.issue_for_message(chat_id, item["message_id"]) is None]
        if not fresh:
            await self._tg.send_message(chat_id, "по этим сообщениям задача уже есть", reply_to=message_id)
            return

        # Забираем их из буфера серии, иначе через полминуты заведётся дубль.
        self._store.drop_burst_parts(chat_id, [item["message_id"] for item in fresh])
        if len(fresh) < count:
            logger.info("/task %d: доступно только %d сообщений", count, len(fresh))
        await self._process(fresh, forced=True)

    # ---- личный список ----------------------------------------------------
    async def _show_vault(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]
        author = message.get("from") or {}
        author_id = int(author.get("id") or 0)

        refs = self._store.issues_of_author(author_id, limit=40)
        if not refs:
            where = "в рабочий чат" if message["chat"].get("type") == "private" else "в этот чат"
            await self._tg.send_message(
                chat_id,
                f"За тобой пока задач нет. Напиши про баг {where} — заведу и покажу здесь.",
                reply_to=message_id,
            )
            return

        states = await self._plane.issue_states()
        buckets: dict[str, list[str]] = {group: [] for group, _ in STATE_SECTIONS}
        missing = 0
        for ref in refs:
            state = states.get(ref.issue_id)
            if state is None:
                missing += 1  # задачу удалили из Plane
                continue
            key = f"{self._plane.identifier}-{ref.sequence_id}"
            title = html.escape(triage.shorten(state.name, 58))
            buckets.setdefault(state.group, []).append(f'• <a href="{html.escape(ref.url)}">{key}</a> {title}')

        shown = sum(len(rows) for rows in buckets.values())
        lines = [f"📋 <b>Задачи {html.escape(triage.author_name(author))}</b> — {shown}"]
        for group, caption in STATE_SECTIONS:
            rows = buckets.get(group) or []
            if rows:
                lines += ["", f"<b>{caption}</b>", *rows]
        if missing:
            lines += ["", f"<i>ещё {missing} удалены из Plane</i>"]

        await self._send_html_or_plain(chat_id, "\n".join(lines), reply_to=message_id)
        logger.info("/my для %s: показано %d задач", author_id, shown)

    # ---- личка ------------------------------------------------------------
    def _watched(self) -> str:
        return ", ".join(f"«{html.escape(title)}»" for title in self._chat_titles.values())

    def _private_help(self) -> str:
        lines = [f"Привет! Превращаю баг-репорты из чата в задачи Plane (проект <b>{self._cfg.plane_project}</b>)."]
        if watched := self._watched():
            lines.append(f"Слежу за: {watched}.")
        lines += [
            "",
            "Здесь, в личке:",
            "• <code>/my</code> — твои задачи и их статусы",
            "• <code>/ping</code> — жив ли я и куда пишу",
            "",
            "Сами задачи заводятся в рабочем чате: напиши там про баг — сделаю задачу "
            "и отвечу ссылкой. Там же работают <code>/task</code>, <code>/skip</code> "
            "и кнопка «🚫 Не баг».",
        ]
        return "\n".join(lines)

    async def _answer_private(self, message: dict[str, Any]) -> None:
        """Личка — личный кабинет, а не источник багов. Отвечаем всегда, но полную
        справку показываем один раз: дальше короткая подсказка, а не простыня."""
        chat_id = message["chat"]["id"]
        text = (
            "Баги пиши в рабочий чат — оттуда заведу задачу.\n"
            "Здесь: <code>/my</code> — твои задачи, <code>/help</code> — что я умею."
            if chat_id in self._dm_seen
            else self._private_help()
        )
        self._dm_seen.add(chat_id)
        await self._tg.send_message(chat_id, text, reply_to=message["message_id"])

    def _ref_for(self, chat_id: int, message_id: int) -> IssueRef | None:
        return self._store.issue_for_message(chat_id, message_id) or self._store.issue_by_bot_message(
            chat_id, message_id
        )

    def _allowed(self, chat_id: int) -> bool:
        return chat_id in self._cfg.chat_ids or chat_id in self._extra_chats

    async def _is_chat_admin(self, chat_id: int, user_id: int) -> bool:
        """Права спрашиваем у Telegram каждый раз: админов назначают и снимают,
        а закешированный ответ означал бы «уволенный вчера всё ещё может».
        Команда редкая и ручная — лишний вызов тут дешевле неверного «да».
        """
        try:
            member = await self._tg.get_chat_member(chat_id, user_id)
        except TelegramError as exc:
            logger.warning("не проверил права %s в %s: %s", user_id, chat_id, exc.description)
            return False
        return member.get("status") in ("creator", "administrator")

    def _note_unknown_chat(self, chat_id: int, chat: dict[str, Any]) -> None:
        if chat_id in self._unknown_chats:
            return
        self._unknown_chats.add(chat_id)
        logger.info(
            "сообщение из чужого чата %s «%s» — игнорирую. Нужен он? допишите id в BUGBOT_CHAT_IDS",
            chat_id,
            chat.get("title") or chat.get("type"),
        )

    # ---- основной сценарий ------------------------------------------------
    async def _process(self, messages: list[dict[str, Any]], *, forced: bool = False) -> None:
        messages = sorted(messages, key=lambda m: m.get("message_id", 0))
        anchor = messages[0]
        chat = anchor.get("chat") or {}
        chat_id = chat["id"]
        message_id = anchor["message_id"]

        text = "\n".join(filter(None, (text_of(m) for m in messages)))
        media = [ref for ref in (media_of(m) for m in messages) if ref is not None]
        author = triage.author_name(anchor.get("from"))
        when = triage.utc_from_unix(anchor.get("date"))
        link = triage.message_link(chat_id, message_id, chat.get("username"))

        if not forced and (parent := anchor.get("reply_to_message")):
            ref = self._ref_for(chat_id, parent["message_id"])
            if ref is not None:
                await self._append_comment(ref, text=text, media=media, author=author, when=when, link=link)
                await self._tg.set_reaction(chat_id, message_id, SEEN_EMOJI)
                return

        # `/task` — осознанное решение человека, триаж тут только мешал бы.
        if not forced and not triage.is_bug_shaped(text, has_media=bool(media), min_text_len=self._cfg.min_text_len):
            logger.info("не баг, пропускаю #%s: %r", message_id, text[:70])
            return

        downloads = await self._download(media)
        images = [pair for pair in downloads if pair[0].is_image]
        files = [pair for pair in downloads if not pair[0].is_image]
        priority = triage.priority_for(text)
        chat_title = chat.get("title") or "личный чат"

        def describe(components: list[str]) -> str:
            return triage.render_description(
                text=text,
                author=author,
                chat_title=chat_title,
                when=when,
                link=link,
                images=components,
                attachments=len(files),
            )

        issue = await self._plane.create_issue(
            name=triage.make_title(text, author=author, has_media=bool(media), when=when, parts=len(messages)),
            description_html=describe([]),
            priority=priority,
        )

        for message in messages:
            self._store.link_issue(
                chat_id=chat_id,
                message_id=message["message_id"],
                issue_id=issue.id,
                sequence_id=issue.sequence_id,
                project_id=self._plane.project_id,
                url=issue.url,
                state_group=self._plane.new_issue_group,
                source_text=text,
                author_id=int((anchor.get("from") or {}).get("id") or 0),
                author_name=author,
            )

        # Картинки — в тело описания: ассет описания привязывается к уже существующей
        # задаче, поэтому грузим после создания и дописываем разметку вторым PATCH-ем.
        components = []
        for ref, got in images:
            asset_id = await self._plane.upload_description_image(
                issue.id, filename=got.filename, mime=got.mime, data=got.data
            )
            if asset_id:
                components.append(plane.image_component(asset_id, width=ref.width, height=ref.height))
        if components:
            await self._plane.update_description(issue.id, describe(components))

        attached = 0
        for _, got in files:
            attached += await self._plane.upload_attachment(
                issue.id, filename=got.filename, mime=got.mime, data=got.data
            )

        logger.info(
            "создана %s (%s): «%s», картинок %d/%d, файлов %d/%d ← сообщение %s",
            issue.key,
            priority,
            issue.name,
            len(components),
            len(images),
            attached,
            len(files),
            message_id,
        )

        reply_text = (
            f'🐞 <a href="{html.escape(issue.url)}">{issue.key}</a> · '
            f"{self._cfg.plane_state} · {PRIORITY_LABEL.get(priority, priority)}"
        )
        if components:
            reply_text += f" · скринов: {len(components)}"
        if attached:
            reply_text += f" · файлов: {attached}"
        lost = (len(images) - len(components)) + (len(files) - attached)
        if lost:
            reply_text += f" · не загрузилось: {lost}"
        reply_text += f"\n<b>{html.escape(issue.name)}</b>"

        try:
            sent = await self._tg.send_message(
                chat_id, reply_text, reply_to=message_id, buttons=[cancel_button(issue.id)]
            )
            self._store.set_bot_message(chat_id, message_id, sent["message_id"])
        except TelegramError as exc:
            logger.warning("задача %s создана, но ответить в чат не вышло: %s", issue.key, exc.description)
        await self._tg.set_reaction(chat_id, message_id, SEEN_EMOJI)

    async def _append_comment(
        self,
        ref: IssueRef,
        *,
        text: str,
        media: list[MediaRef],
        author: str,
        when,
        link: str | None,
    ) -> None:
        downloads = await self._download(media)
        for _, got in downloads:
            await self._plane.upload_attachment(ref.issue_id, filename=got.filename, mime=got.mime, data=got.data)
        comment_id = await self._plane.add_comment(
            ref.issue_id,
            triage.render_comment(text=text, author=author, when=when, link=link, attachments=len(downloads)),
        )
        # Помечаем свой комментарий: при закрытии задачи он не должен вернуться в чат как «что сделали».
        self._store.remember_bot_comment(ref.issue_id, comment_id)
        logger.info("комментарий к задаче #%s (вложений %d)", ref.sequence_id, len(downloads))

    async def _on_edit(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat["id"]
        ref = self._store.issue_for_message(chat_id, message["message_id"])
        if ref is None:
            return
        await self._plane.update_description(
            ref.issue_id,
            triage.render_description(
                text=text_of(message),
                author=triage.author_name(message.get("from")),
                chat_title=chat.get("title") or "личный чат",
                when=triage.utc_from_unix(message.get("edit_date") or message.get("date")),
                link=triage.message_link(chat_id, message["message_id"], chat.get("username")),
                edited=True,
            ),
        )
        logger.info("описание задачи #%s обновлено после правки сообщения", ref.sequence_id)

    async def _on_reaction(self, reaction: dict[str, Any]) -> None:
        chat_id = (reaction.get("chat") or {}).get("id")
        message_id = reaction.get("message_id")
        if chat_id is None or message_id is None or not self._allowed(chat_id):
            return
        emojis = {item.get("emoji") for item in reaction.get("new_reaction") or [] if item.get("type") == "emoji"}
        if not emojis & CANCEL_EMOJI:
            return
        ref = self._ref_for(chat_id, message_id)
        if ref is None or ref.cancelled:
            return
        await self._cancel(ref, reason="реакция 👎")

    async def _cancel(self, ref: IssueRef, *, reason: str) -> None:
        try:
            await self._plane.set_state(ref.issue_id, self._cfg.plane_cancel_state)
        except PlaneError as exc:
            logger.error("не смог отменить задачу #%s: %s", ref.sequence_id, exc)
            return
        self._store.mark_cancelled(ref.issue_id)
        logger.info("задача #%s отменена (%s)", ref.sequence_id, reason)

        text = f'🚫 <a href="{html.escape(ref.url)}">{self._plane.identifier}-{ref.sequence_id}</a> — не баг, отменено'
        if ref.bot_message_id:
            # buttons не передаём: кнопка снимается, второй раз жать нечего.
            await self._tg.edit_message_text(ref.chat_id, ref.bot_message_id, text)
        else:
            await self._tg.send_message(ref.chat_id, text, reply_to=ref.message_id)

    async def _download(self, media: list[MediaRef]) -> list[tuple[MediaRef, Download]]:
        """Пары (описание медиа, файл): дальше по `is_image` решаем — в тело или во вложения."""
        out: list[tuple[MediaRef, Download]] = []
        for item in media:
            got = await self._tg.download(
                item.file_id,
                max_bytes=self._cfg.max_attachment_bytes,
                fallback_name=item.filename,
                mime=item.mime,
            )
            if got is not None:
                out.append((item, got))
        return out
