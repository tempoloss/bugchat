"""Маршрутизация команд: меню по «/», личка и чужие чаты."""

import asyncio
from pathlib import Path
import re

import pytest

from bugbot import app
from bugbot.config import Config
from bugbot.plane import IssueState
from bugbot.telegram import TelegramError

GROUP_ID = -100
DM_ID = 500


class FakeTelegram:
    """Тот же интерфейс, что у TelegramClient, но без сети."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.member_status = "member"

    async def send_message(self, chat_id: int, text: str, *, reply_to=None, buttons=None) -> dict:
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    async def set_reaction(self, *_args, **_kwargs) -> None:
        pass

    async def get_chat_member(self, _chat_id: int, _user_id: int) -> dict:
        return {"status": self.member_status}

    async def aclose(self) -> None:
        pass


def make_config(db_path: Path) -> Config:
    return Config(
        bot_token="token",
        chat_ids=frozenset({GROUP_ID}),
        plane_base="https://plane.invalid",
        plane_workspace="acme",
        plane_email="e",
        plane_password="p",
        plane_project="BL",
        plane_state="Backlog",
        plane_cancel_state="Cancelled",
        plane_labels=("telegram",),
        plane_verify_tls=False,
        db_path=db_path,
        min_text_len=120,
        album_debounce_s=2.0,
        poll_timeout_s=25,
        max_attachment_bytes=1024,
        close_poll_s=0.0,
        burst_quiet_s=20.0,
        message_log_days=7.0,
    )


@pytest.fixture
def bot(tmp_path):
    instance = app.BugBot(make_config(tmp_path / "state.db"))
    live = instance._tg
    instance._tg = FakeTelegram()
    yield instance
    asyncio.run(live.aclose())
    asyncio.run(instance.aclose())


def message(text: str, *, chat_id: int, chat_type: str, message_id: int = 1) -> dict:
    return {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": chat_type, "title": "чат"},
        "from": {"id": 42, "first_name": "Иван", "username": "ivan"},
        "text": text,
    }


def dm(text: str, message_id: int = 1) -> dict:
    return message(text, chat_id=DM_ID, chat_type="private", message_id=message_id)


def deliver(bot, payload: dict, *, edited: bool = False) -> None:
    asyncio.run(bot._on_message(payload, edited=edited))


# ---- меню по «/» ------------------------------------------------------------
def test_menu_only_offers_commands_the_bot_understands():
    """Клиент показывает меню как обещание: команда из него обязана работать."""
    for menu in (app.GROUP_MENU, app.PRIVATE_MENU):
        for name, description in menu:
            assert re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name), name
            assert 0 < len(description) <= 256, name
            assert f"/{name}" in app.COMMANDS, name


def test_menu_covers_every_standalone_command():
    """Новая команда без строки в меню - невидимка: по «/» её не найдут.
    Алиасы (`/tasks`, `/задача`) в меню не нужны - они дубли уже показанных."""
    aliases = (app.VAULT_COMMANDS | app.TASK_COMMANDS) - {"/my", "/task"}
    standalone = {name for name in app.COMMANDS if name[1:].isascii()} - aliases - {"/start"}
    assert {f"/{name}" for name, _ in app.ADMIN_MENU} == standalone


def test_task_is_offered_only_to_admins():
    """Меню - обещание: рядовому участнику нельзя показывать то, что ему откажут."""
    assert "task" not in {name for name, _ in app.GROUP_MENU}
    assert "task" in {name for name, _ in app.ADMIN_MENU}
    assert set(app.GROUP_MENU) < set(app.ADMIN_MENU)


# ---- личка ------------------------------------------------------------------
def test_private_message_gets_help_then_a_short_hint(bot):
    """Молчать в личке нельзя, но и пересказывать справку на каждое сообщение незачем."""
    deliver(bot, dm("привет"))
    deliver(bot, dm("а ты кто", message_id=2))

    (_, first), (_, second) = bot._tg.sent
    assert "/my" in first and "Привет!" in first
    assert "/my" in second
    assert len(second) < len(first)


def test_private_help_command_counts_as_the_full_greeting(bot):
    """После /help второе сообщение уже не должно повторять ту же простыню."""
    deliver(bot, dm("/help"))
    deliver(bot, dm("ага", message_id=2))

    (_, first), (_, second) = bot._tg.sent
    assert "Привет!" in first
    assert "Привет!" not in second


def test_private_edit_stays_silent(bot):
    """Человек поправил опечатку в личке - это не повод отвечать снова."""
    deliver(bot, dm("привет"))
    deliver(bot, dm("привет!", message_id=1), edited=True)
    assert len(bot._tg.sent) == 1


def test_chat_only_command_in_private_explains_itself(bot):
    """`/task` в личке бессмыслен, но тишина выглядит поломкой - объясняем."""
    deliver(bot, dm("/task"))
    _, text = bot._tg.sent[-1]
    assert "рабочем чате" in text and "/my" in text


def test_vault_works_in_private(bot):
    """Личный список - главное, зачем в личку вообще пишут."""
    deliver(bot, dm("/my"))
    _, text = bot._tg.sent[-1]
    assert "задач нет" in text and "в рабочий чат" in text


# ---- чужие и рабочие чаты ---------------------------------------------------
def test_unlisted_group_stays_silent(bot):
    """В чужую группу бота никто не звал работать - там он не разговаривает."""
    deliver(bot, message("привет", chat_id=-999, chat_type="supergroup"))
    deliver(bot, message("/task", chat_id=-999, chat_type="supergroup", message_id=2))
    assert bot._tg.sent == []


def test_group_help_is_about_the_chat_not_the_dm(bot):
    deliver(bot, message("/help", chat_id=GROUP_ID, chat_type="supergroup"))
    _, text = bot._tg.sent[-1]
    assert "в этом чате" in text
    assert "Привет!" not in text


def test_bots_are_ignored_everywhere(bot):
    """Иначе два бота в чате могут заговорить друг с другом до упора."""
    payload = dm("/help")
    payload["from"]["is_bot"] = True
    deliver(bot, payload)
    assert bot._tg.sent == []


# ---- права на /task ----------------------------------------------------------
def group(text: str, message_id: int = 1) -> dict:
    payload = message(text, chat_id=GROUP_ID, chat_type="supergroup", message_id=message_id)
    payload["reply_to_message"] = message("баг какой-то", chat_id=GROUP_ID, chat_type="supergroup", message_id=99)
    return payload


def test_task_from_regular_member_is_refused(bot):
    """`/task` обходит триаж - рядовой участник им доску не засыплет."""
    bot._tg.member_status = "member"
    deliver(bot, group("/task 4"))

    _, text = bot._tg.sent[-1]
    assert "для админов чата" in text
    assert bot._store.issue_for_message(GROUP_ID, 99) is None


def test_task_from_admin_passes_the_gate(bot):
    """Админа гейт пропускает - дальше работает обычная логика команды."""
    bot._tg.member_status = "administrator"
    payload = group("/task")
    del payload["reply_to_message"]
    deliver(bot, payload)

    _, text = bot._tg.sent[-1]
    assert "ответьте" in text  # подсказка про reply, а не отказ по правам


def test_unverifiable_rights_deny(bot):
    """Telegram не ответил про права - считаем «нет»: тихий пропуск хуже отказа."""

    async def boom(*_args, **_kwargs):
        raise TelegramError("getChatMember", 400, "user not found")

    bot._tg.get_chat_member = boom
    deliver(bot, group("/task"))
    _, text = bot._tg.sent[-1]
    assert "для админов чата" in text


# ---- закрытия в чат, за которым больше не следим -----------------------------
def test_closure_is_not_announced_into_an_unwatched_chat(bot):
    """Чат убрали из BUGBOT_CHAT_IDS — бот туда не пишет даже про свои старые задачи."""
    bot._store.link_issue(
        chat_id=-999,
        message_id=1,
        issue_id="old",
        sequence_id=1,
        project_id="p",
        url="https://plane/1",
        state_group="backlog",
        source_text="",
        author_id=1,
        author_name="кто-то",
    )

    async def states() -> dict[str, IssueState]:
        return {"old": IssueState(state_id="s", group="completed", updated_by=None, name="задача")}

    bot._plane.issue_states = states
    asyncio.run(bot._check_closures())

    assert bot._tg.sent == []
    # Состояние всё равно записано: вернут чат - не хлынет пачка старых уведомлений.
    assert bot._store.issue_by_id("old").last_state_group == "completed"
