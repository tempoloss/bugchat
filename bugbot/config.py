"""Конфигурация бота. Единственный источник — окружение + .env рядом с пакетом."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().strip('"').lower() in ("1", "true", "yes", "on")


def _text(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip().strip('"').strip("'")


def _number(name: str, default: float) -> float:
    raw = _text(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"{name}: ожидалось число, получено {raw!r}") from None


def _chat_ids(name: str) -> frozenset[int]:
    raw = _text(name).replace(";", ",")
    out: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            raise SystemExit(f"{name}: {chunk!r} не похоже на chat_id") from None
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class Config:
    bot_token: str
    chat_ids: frozenset[int]
    """Пустое множество = бот не привязан к чату: отвечает только на /chatid и /ping."""

    plane_base: str
    plane_workspace: str
    plane_email: str
    plane_password: str
    plane_project: str
    plane_state: str
    plane_cancel_state: str
    plane_labels: tuple[str, ...]
    plane_verify_tls: bool

    db_path: Path
    min_text_len: int
    album_debounce_s: float
    poll_timeout_s: int
    max_attachment_bytes: int
    close_poll_s: float
    """Как часто спрашивать Plane про закрытые задачи. 0 — не следить вовсе."""
    burst_quiet_s: float
    """Сколько молчания после сообщения ждать, прежде чем считать серию законченной."""
    message_log_days: float
    """Сколько дней помнить сообщения чата, чтобы `/task N` дотянулся до соседних."""

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv(ROOT / ".env")

        token = _text("BUGBOT_TELEGRAM_TOKEN")
        if not token:
            raise SystemExit("BUGBOT_TELEGRAM_TOKEN не задан — скопируйте .env.example в .env")

        email, password = _text("PLANE_EMAIL"), _text("PLANE_PASSWORD")
        if not email or not password:
            raise SystemExit("PLANE_EMAIL / PLANE_PASSWORD не заданы — скопируйте .env.example в .env")

        # У этих трёх нет значений по умолчанию нарочно: подставленный хост или
        # воркспейс — это тихий логин не туда, куда человек думает. Пусть падает
        # на старте, а не заводит задачи в чужом проекте.
        base = _text("PLANE_BASE", "").rstrip("/")
        workspace = _text("PLANE_WORKSPACE", "")
        project = _text("BUGBOT_PLANE_PROJECT", "")
        missing = [
            name
            for name, value in (
                ("PLANE_BASE", base),
                ("PLANE_WORKSPACE", workspace),
                ("BUGBOT_PLANE_PROJECT", project),
            )
            if not value
        ]
        if missing:
            raise SystemExit(f"не заданы: {', '.join(missing)} — смотрите .env.example")

        db_raw = _text("BUGBOT_DB", "state.db")
        db_path = Path(db_raw) if Path(db_raw).is_absolute() else ROOT / db_raw

        labels = tuple(x.strip() for x in _text("BUGBOT_PLANE_LABELS", "telegram,bug").split(",") if x.strip())

        return cls(
            bot_token=token,
            chat_ids=_chat_ids("BUGBOT_CHAT_IDS"),
            plane_base=base,
            plane_workspace=workspace,
            plane_email=email,
            plane_password=password,
            plane_project=project,
            plane_state=_text("BUGBOT_PLANE_STATE", "Backlog"),
            plane_cancel_state=_text("BUGBOT_PLANE_CANCEL_STATE", "Cancelled"),
            plane_labels=labels,
            # PLANE_INSECURE=1 уже стоит в окружении у большинства — внутренний CA не в trust store.
            plane_verify_tls=not _flag("PLANE_INSECURE", False),
            db_path=db_path,
            min_text_len=int(_number("BUGBOT_MIN_TEXT_LEN", 120)),
            album_debounce_s=_number("BUGBOT_ALBUM_DEBOUNCE", 2.0),
            poll_timeout_s=int(_number("BUGBOT_POLL_TIMEOUT", 25)),
            max_attachment_bytes=int(_number("BUGBOT_MAX_ATTACHMENT_MB", 20) * 1024 * 1024),
            close_poll_s=_number("BUGBOT_CLOSE_POLL_SECONDS", 60),
            burst_quiet_s=_number("BUGBOT_BURST_SECONDS", 20),
            message_log_days=_number("BUGBOT_MESSAGE_LOG_DAYS", 7),
        )
