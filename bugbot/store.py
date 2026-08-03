"""Состояние бота в SQLite: offset апдейтов, карта сообщение→задача, буфер альбомов, dead-letter.

Всё синхронное: запросы локальные и занимают микросекунды, гонять их через
executor дороже, чем выполнить. Соединение одно, `check_same_thread=False`
не нужен — работаем из одного event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    chat_id        INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    issue_id       TEXT    NOT NULL,
    sequence_id    INTEGER NOT NULL,
    project_id     TEXT    NOT NULL,
    url            TEXT    NOT NULL,
    bot_message_id INTEGER,
    cancelled      INTEGER NOT NULL DEFAULT 0,
    created_at     REAL    NOT NULL,
    last_state_group TEXT,
    source_text    TEXT,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS ix_issues_bot_message ON issues (chat_id, bot_message_id);
CREATE INDEX IF NOT EXISTS ix_issues_issue ON issues (issue_id);

CREATE TABLE IF NOT EXISTS bot_comments (
    comment_id TEXT PRIMARY KEY,
    issue_id   TEXT NOT NULL,
    at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS processed (
    update_id INTEGER PRIMARY KEY,
    at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS album_parts (
    media_group_id TEXT    NOT NULL,
    message_id     INTEGER NOT NULL,
    chat_id        INTEGER NOT NULL,
    payload        TEXT    NOT NULL,
    received_at    REAL    NOT NULL,
    PRIMARY KEY (media_group_id, message_id)
);

CREATE TABLE IF NOT EXISTS burst_parts (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    payload    TEXT    NOT NULL,
    at         REAL    NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    at         REAL    NOT NULL,
    payload    TEXT    NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS deadletter (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id INTEGER,
    payload   TEXT NOT NULL,
    error     TEXT NOT NULL,
    at        REAL NOT NULL
);
"""

_PROCESSED_KEEP = 5000
"""Сколько update_id держим для защиты от повторной обработки. Offset и так
двигается, эта таблица нужна только на случай падения между ack и обработкой."""


@dataclass(frozen=True, slots=True)
class IssueRef:
    chat_id: int
    message_id: int
    issue_id: str
    sequence_id: int
    project_id: str
    url: str
    bot_message_id: int | None
    cancelled: bool
    last_state_group: str | None
    """Группа состояния Plane при прошлом опросе: по смене шлём ответ в чат."""
    source_text: str
    """Текст исходного сообщения — чтобы при закрытии не пересказывать его автору."""
    author_id: int
    """Telegram-id того, кто прислал баг: по нему собирается личный список задач."""
    author_name: str


def _ref(row: sqlite3.Row) -> IssueRef:
    return IssueRef(
        chat_id=row["chat_id"],
        message_id=row["message_id"],
        issue_id=row["issue_id"],
        sequence_id=row["sequence_id"],
        project_id=row["project_id"],
        url=row["url"],
        bot_message_id=row["bot_message_id"],
        cancelled=bool(row["cancelled"]),
        last_state_group=row["last_state_group"],
        source_text=row["source_text"] or "",
        author_id=row["author_id"] or 0,
        author_name=row["author_name"] or "",
    )


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Схема доезжает на живой базе: state.db переживает обновления бота."""
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(issues)")}
        for column, ddl in (
            ("last_state_group", "TEXT"),
            ("source_text", "TEXT"),
            ("author_id", "INTEGER"),
            ("author_name", "TEXT"),
        ):
            if column not in columns:
                self._db.execute(f"ALTER TABLE issues ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self._db.close()

    # ---- offset ---------------------------------------------------------
    @property
    def offset(self) -> int:
        row = self._db.execute("SELECT value FROM meta WHERE key = 'offset'").fetchone()
        return int(row["value"]) if row else 0

    @offset.setter
    def offset(self, value: int) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES ('offset', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(value),),
        )

    # ---- идемпотентность ------------------------------------------------
    def is_processed(self, update_id: int) -> bool:
        return self._db.execute("SELECT 1 FROM processed WHERE update_id = ?", (update_id,)).fetchone() is not None

    def mark_processed(self, update_id: int) -> None:
        self._db.execute("INSERT OR IGNORE INTO processed (update_id, at) VALUES (?, ?)", (update_id, time.time()))
        self._db.execute(
            "DELETE FROM processed WHERE update_id <= (SELECT MAX(update_id) - ? FROM processed)",
            (_PROCESSED_KEEP,),
        )

    # ---- задачи ---------------------------------------------------------
    def link_issue(
        self,
        *,
        chat_id: int,
        message_id: int,
        issue_id: str,
        sequence_id: int,
        project_id: str,
        url: str,
        state_group: str,
        source_text: str = "",
        author_id: int = 0,
        author_name: str = "",
    ) -> IssueRef:
        self._db.execute(
            """INSERT INTO issues
                   (chat_id, message_id, issue_id, sequence_id, project_id, url, created_at,
                    last_state_group, source_text, author_id, author_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id, message_id) DO UPDATE SET
                   issue_id = excluded.issue_id, sequence_id = excluded.sequence_id, url = excluded.url,
                   -- Перепривязка сообщения к другой задаче обязана сбросить и трекинг:
                   -- иначе к новой задаче прилипнут исходный текст и состояние прежней.
                   source_text = excluded.source_text, last_state_group = excluded.last_state_group,
                   author_id = excluded.author_id, author_name = excluded.author_name,
                   cancelled = 0, bot_message_id = NULL""",
            (
                chat_id,
                message_id,
                issue_id,
                sequence_id,
                project_id,
                url,
                time.time(),
                state_group,
                source_text,
                author_id,
                author_name,
            ),
        )
        found = self.issue_for_message(chat_id, message_id)
        assert found is not None  # только что вставили
        return found

    def issues_of_author(self, author_id: int, *, limit: int = 50) -> list[IssueRef]:
        """Личный список: по одной строке на задачу, свежие сверху."""
        rows = self._db.execute(
            """SELECT * FROM issues WHERE author_id = ? AND message_id IN
                   (SELECT MIN(message_id) FROM issues GROUP BY issue_id)
               -- Тайбрейк по message_id обязателен: на Windows несколько задач,
               -- заведённых в один тик часов, иначе выстраивались бы произвольно.
               ORDER BY created_at DESC, message_id DESC LIMIT ?""",
            (author_id, limit),
        ).fetchall()
        return [_ref(row) for row in rows]

    def tracked_issues(self) -> list[IssueRef]:
        """По одной строке на задачу — якорное (первое) сообщение, в него и отвечаем."""
        rows = self._db.execute(
            """SELECT * FROM issues WHERE message_id IN
                   (SELECT MIN(message_id) FROM issues GROUP BY issue_id)"""
        ).fetchall()
        return [_ref(row) for row in rows]

    def issue_by_id(self, issue_id: str) -> IssueRef | None:
        """Якорная строка задачи — по ней отвечаем на нажатие кнопки в чате."""
        row = self._db.execute(
            "SELECT * FROM issues WHERE issue_id = ? ORDER BY message_id LIMIT 1", (issue_id,)
        ).fetchone()
        return _ref(row) if row else None

    def last_open_issue(self, chat_id: int) -> IssueRef | None:
        """Последняя незакрытая задача чата — цель для `/skip` без ответа на сообщение."""
        row = self._db.execute(
            """SELECT * FROM issues WHERE chat_id = ? AND cancelled = 0
               ORDER BY created_at DESC, message_id DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()
        return _ref(row) if row else None

    def remember_bot_comment(self, issue_id: str, comment_id: str) -> None:
        """Свои комментарии помним, чтобы не пересказать их обратно в чат при закрытии."""
        self._db.execute(
            "INSERT OR IGNORE INTO bot_comments (comment_id, issue_id, at) VALUES (?, ?, ?)",
            (comment_id, issue_id, time.time()),
        )

    def bot_comment_ids(self, issue_id: str) -> set[str]:
        rows = self._db.execute("SELECT comment_id FROM bot_comments WHERE issue_id = ?", (issue_id,))
        return {row["comment_id"] for row in rows}

    def set_state_group(self, issue_id: str, group: str) -> None:
        self._db.execute("UPDATE issues SET last_state_group = ? WHERE issue_id = ?", (group, issue_id))

    def set_bot_message(self, chat_id: int, message_id: int, bot_message_id: int) -> None:
        self._db.execute(
            "UPDATE issues SET bot_message_id = ? WHERE chat_id = ? AND message_id = ?",
            (bot_message_id, chat_id, message_id),
        )

    def mark_cancelled(self, issue_id: str) -> None:
        """По issue_id, а не по сообщению: у альбома на одну задачу несколько строк,
        и отмена по любой части должна гасить их все — иначе 👎 по второму скриншоту
        отменит уже отменённую задачу и повторно напишет в чат."""
        self._db.execute("UPDATE issues SET cancelled = 1 WHERE issue_id = ?", (issue_id,))

    def issue_for_message(self, chat_id: int, message_id: int) -> IssueRef | None:
        row = self._db.execute(
            "SELECT * FROM issues WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
        ).fetchone()
        return _ref(row) if row else None

    def issue_by_bot_message(self, chat_id: int, bot_message_id: int) -> IssueRef | None:
        row = self._db.execute(
            "SELECT * FROM issues WHERE chat_id = ? AND bot_message_id = ?", (chat_id, bot_message_id)
        ).fetchone()
        return _ref(row) if row else None

    # ---- альбомы --------------------------------------------------------
    def add_album_part(self, media_group_id: str, chat_id: int, message_id: int, payload: dict[str, Any]) -> None:
        self._db.execute(
            """INSERT OR IGNORE INTO album_parts (media_group_id, message_id, chat_id, payload, received_at)
               VALUES (?, ?, ?, ?, ?)""",
            (media_group_id, message_id, chat_id, json.dumps(payload, ensure_ascii=False), time.time()),
        )

    def due_albums(self, debounce_s: float) -> list[str]:
        """Группы, в которые `debounce_s` секунд не прилетало новых частей."""
        rows = self._db.execute(
            "SELECT media_group_id FROM album_parts GROUP BY media_group_id HAVING MAX(received_at) <= ?",
            (time.time() - debounce_s,),
        ).fetchall()
        return [row["media_group_id"] for row in rows]

    def pop_album(self, media_group_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT payload FROM album_parts WHERE media_group_id = ? ORDER BY message_id",
            (media_group_id,),
        ).fetchall()
        self._db.execute("DELETE FROM album_parts WHERE media_group_id = ?", (media_group_id,))
        return [json.loads(row["payload"]) for row in rows]

    # ---- серия сообщений от одного автора --------------------------------
    def add_burst_part(self, chat_id: int, user_id: int, message_id: int, payload: dict[str, Any]) -> None:
        """Копим сообщения автора: один баг часто расписывают тремя подряд."""
        self._db.execute(
            """INSERT OR REPLACE INTO burst_parts (chat_id, message_id, user_id, payload, at)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, message_id, user_id, json.dumps(payload, ensure_ascii=False), time.time()),
        )

    def due_bursts(self, quiet_s: float) -> list[tuple[int, int]]:
        """Пары (чат, автор), от которых `quiet_s` секунд тишины — серия закончилась."""
        rows = self._db.execute(
            """SELECT chat_id, user_id FROM burst_parts
               GROUP BY chat_id, user_id HAVING MAX(at) <= ?""",
            (time.time() - quiet_s,),
        ).fetchall()
        return [(row["chat_id"], row["user_id"]) for row in rows]

    def pop_burst(self, chat_id: int, user_id: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT payload FROM burst_parts WHERE chat_id = ? AND user_id = ? ORDER BY message_id",
            (chat_id, user_id),
        ).fetchall()
        self._db.execute("DELETE FROM burst_parts WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        return [json.loads(row["payload"]) for row in rows]

    def drop_burst_parts(self, chat_id: int, message_ids: list[int]) -> None:
        """Убирает сообщения из буфера: их забрал `/task`, второй раз заводить не нужно."""
        if not message_ids:
            return
        marks = ",".join("?" * len(message_ids))
        self._db.execute(
            f"DELETE FROM burst_parts WHERE chat_id = ? AND message_id IN ({marks})",
            (chat_id, *message_ids),
        )

    # ---- журнал сообщений чата -------------------------------------------
    def log_message(self, chat_id: int, message_id: int, user_id: int, payload: dict[str, Any]) -> None:
        """Bot API не умеет доставать сообщение по id, поэтому храним увиденное сами —
        без этого `/task 4` не смог бы дотянуться до трёх следующих сообщений."""
        self._db.execute(
            """INSERT OR REPLACE INTO chat_messages (chat_id, message_id, user_id, at, payload)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, message_id, user_id, time.time(), json.dumps(payload, ensure_ascii=False)),
        )

    def messages_from(self, chat_id: int, first_message_id: int, count: int) -> list[dict[str, Any]]:
        """Сообщение и следующие за ним — ровно то, что просит `/task N`."""
        rows = self._db.execute(
            """SELECT payload FROM chat_messages WHERE chat_id = ? AND message_id >= ?
               ORDER BY message_id LIMIT ?""",
            (chat_id, first_message_id, count),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def prune_messages(self, older_than_s: float) -> int:
        # `<=`, как в due_albums/due_bursts: на Windows time.time() тикает раз в ~15 мс,
        # со строгим `<` только что записанная строка не попадала бы под нулевой порог.
        cursor = self._db.execute("DELETE FROM chat_messages WHERE at <= ?", (time.time() - older_than_s,))
        return cursor.rowcount or 0

    # ---- dead-letter ----------------------------------------------------
    def dead_letter(self, update_id: int | None, payload: dict[str, Any], error: str) -> None:
        self._db.execute(
            "INSERT INTO deadletter (update_id, payload, error, at) VALUES (?, ?, ?, ?)",
            (update_id, json.dumps(payload, ensure_ascii=False), error, time.time()),
        )
