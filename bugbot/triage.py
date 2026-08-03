"""Чистая логика разбора сообщений: баг это или болтовня, заголовок, приоритет, тело задачи.

Ни одного сетевого вызова — весь модуль покрыт юнит-тестами.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import html
import re
from typing import Any

_BUG_RE = re.compile(
    r"ошибк|ошибает|не\s+работ|не\s+открыв|не\s+грузит|не\s+загруж|не\s+сохран|не\s+отображ|не\s+выгруж"
    r"|не\s+находит|не\s+даёт|не\s+дает|не\s+приходит|не\s+прогруж|перестал|падает|упал|лежит|вис[ин]"
    r"|зависа|тормоз|краш|вылет|сломал|поломал|битый|некоррект|неправильн|дубл|пуст[оа]й\s+ответ"
    # «фикс» сюда не входит: «пофиксил» — это отчёт о починке, а не жалоба.
    r"|баг|глюк|traceback|exception|stacktrace|internal\s+server|timeout|таймаут"
    r"|\b(?:4\d{2}|5\d{2})\s*(?:ошибка|error|response)?\b|\berror\b|\bfail(?:ed|s)?\b|\bbug\b",
    re.IGNORECASE,
)
"""Лексика жалобы. Одного попадания достаточно, чтобы завести задачу."""

_URGENT_RE = re.compile(
    r"срочн|критич|блокер|blocker|критикал|горит|прод\w*\s+лежит|лежит\s+прод|всё\s+упало|все\s+упало"
    r"|ничего\s+не\s+работает|не\s+работает\s+вообще|аврал|asap",
    re.IGNORECASE,
)

_RESOLVED_RE = re.compile(
    r"уже\s+работает|всё\s+работает|все\s+работает|заработал|исправил|исправлен|починил|пофиксил"
    r"|поправил|задеплоил|выкатил|готово|решено|закрыл",
    re.IGNORECASE,
)
"""Отчёт «починено». Такое не заводим — если только в том же тексте нет новой жалобы."""

_TITLE_TRIM_RE = re.compile(r"^[\s\-—–*•>#]+|[\s\-—–*•]+$")
_HASHTAG_RE = re.compile(r"#\w+")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\u2190-\u21ff\u2300-\u27bf\u2b00-\u2bff\ufe0f\u200d\u20e3]+",
)
_OPENER_RE = re.compile(
    r"^(?:@\w+|привет(?:ики)?|здравствуйте|добрый\s+день|добрый\s+вечер|доброе\s+утро|коллеги|ребят[а]?"
    r"|народ|пацаны|слушай(?:те)?|смотри(?:те)?|кароч[еe]|короче|блин|срочно|внимание|апд|upd|пж|плиз)"
    r"\b[\s,:!;—-]*",
    re.IGNORECASE,
)
"""Обращения и вводные слова в начале сообщения — в заголовке они лишние."""
_PUNCT_RUN_RE = re.compile(r"[!?.]{2,}|!+$")
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s)")

TITLE_LIMIT = 80
_TITLE_MIN_INFORMATIVE = 15
"""Короче этого заголовок ничего не объясняет — дополняем автором и датой."""
_TITLE_ENOUGH = 40
"""Достаточная длина, чтобы перестать добирать сообщения серии в заголовок."""


def is_bug_shaped(text: str, *, has_media: bool, min_text_len: int) -> bool:
    """Стоит ли заводить задачу по этому сообщению."""
    normalized = text.strip()
    has_bug_words = bool(_BUG_RE.search(normalized))

    # «Починил» без новой жалобы — это отчёт, а не баг, даже если приложен скрин.
    if _RESOLVED_RE.search(normalized) and not has_bug_words:
        return False
    if has_bug_words:
        return True
    if has_media:
        return True
    return len(normalized) >= min_text_len


def priority_for(text: str) -> str:
    """Приоритет Plane: urgent / high / none."""
    if _URGENT_RE.search(text):
        return "urgent"
    if _BUG_RE.search(text):
        return "high"
    return "none"


def author_name(user: dict[str, Any] | None) -> str:
    if not user:
        return "неизвестный автор"
    parts = [user.get("first_name") or "", user.get("last_name") or ""]
    name = " ".join(p for p in parts if p).strip()
    username = user.get("username")
    if name and username:
        return f"{name} (@{username})"
    if username:
        return f"@{username}"
    if name:
        return f"{name} (id {user.get('id')})"
    return f"id {user.get('id')}"


def make_title(text: str, *, author: str, has_media: bool, when: datetime, parts: int = 1) -> str:
    """Человеческий заголовок задачи из сообщения в чате.

    Сырая первая строка не годится: люди пишут «ребят, кароче не работает!!!»,
    а на доске нужен «Не работает». Порядок: выкинуть обращения и мусор,
    обрезать по концу предложения, поднять первую букву, а слишком короткий
    заголовок («не работает») дополнить автором и датой — иначе три таких
    задачи на доске неразличимы.

    `parts` — из скольких сообщений склеен текст. Когда мысль разбита на
    несколько сообщений, первая строка почти всегда обрывок («а можно в карточке»),
    поэтому строки добираются, пока фраза не станет осмысленной.
    """
    line = _clean_title(_pick_line(text, greedy=parts > 1))

    if not line:
        kind = "скриншот" if has_media else "сообщение"
        return shorten(f"Баг из Telegram: {kind} от {author}, {when:%d.%m %H:%M}", TITLE_LIMIT)

    line = _cut_sentence(line)
    line = line[0].upper() + line[1:]
    if len(line) < _TITLE_MIN_INFORMATIVE:
        line = f"{line} — {author}, {when:%d.%m %H:%M}"
    return shorten(line, TITLE_LIMIT)


def _pick_line(text: str, *, greedy: bool = False) -> str:
    """Первая содержательная строка; если она куцая — добираем следующие."""
    lines = [_clean_title(raw) for raw in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    head = lines[0]
    if greedy:
        # Сообщения серии — куски одной фразы, склеиваются пробелом и читаются подряд.
        for extra in lines[1:]:
            if len(head) >= _TITLE_ENOUGH:
                break
            head = f"{head} {extra}"
        return head
    if len(head) < _TITLE_MIN_INFORMATIVE and len(lines) > 1:
        return f"{head.rstrip(':')}: {lines[1].lstrip(':').strip()}"
    return head


def _clean_title(line: str) -> str:
    line = _EMOJI_RE.sub("", _HASHTAG_RE.sub("", line))
    line = _WS_RE.sub(" ", line).strip()
    line = _TITLE_TRIM_RE.sub("", line)

    # «Ребят, привет, не грузится» → «не грузится»: обращения снимаем по одному.
    while True:
        stripped = _OPENER_RE.sub("", line, count=1)
        if stripped == line:
            break
        line = stripped.lstrip(" ,:;—-")

    line = _PUNCT_RUN_RE.sub(lambda m: "?" if "?" in m.group() else "", line)
    return line.strip(" ,.;:!—-*•>")


def _cut_sentence(line: str) -> str:
    """Если первое предложение уже самодостаточно — берём только его."""
    match = _SENTENCE_END_RE.search(line)
    if match and _TITLE_MIN_INFORMATIVE <= match.start() + 1 <= TITLE_LIMIT:
        return line[: match.start() + 1].rstrip(" .!")
    return line


def shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    cut = value[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def message_link(chat_id: int, message_id: int, chat_username: str | None) -> str | None:
    """Ссылка на сообщение. Для приватных супергрупп — форма t.me/c/<internal>/<id>."""
    if chat_username:
        return f"https://t.me/{chat_username}/{message_id}"
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    return None


def _paragraphs(text: str) -> str:
    escaped = html.escape(text.strip())
    return "<br/>".join(escaped.splitlines())


def render_description(
    *,
    text: str,
    author: str,
    chat_title: str,
    when: datetime,
    link: str | None,
    images: Sequence[str] = (),
    attachments: int = 0,
    edited: bool = False,
) -> str:
    body = _paragraphs(text) or "<i>без текста</i>"
    meta = [
        f"<b>Автор:</b> {html.escape(author)}",
        f"<b>Чат:</b> {html.escape(chat_title)}",
        f"<b>Время:</b> {when:%d.%m.%Y %H:%M} UTC",
    ]
    if attachments:
        meta.append(f"<b>Вложений:</b> {attachments}")
    if link:
        meta.append(f'<b>Сообщение:</b> <a href="{html.escape(link)}">{html.escape(link)}</a>')
    if edited:
        meta.append("<i>Описание обновлено после правки сообщения в Telegram.</i>")
    # Картинки идут сразу за текстом, до служебного блока — так их видно первым делом.
    return f"<p>{body}</p>{''.join(images)}<hr/><p>{'<br/>'.join(meta)}</p>"


def render_comment(*, text: str, author: str, when: datetime, link: str | None, attachments: int = 0) -> str:
    body = _paragraphs(text) or "<i>без текста</i>"
    tail = f"— {html.escape(author)}, {when:%d.%m %H:%M} UTC"
    if attachments:
        tail += f", вложений: {attachments}"
    if link:
        tail += f' · <a href="{html.escape(link)}">сообщение</a>'
    return f"<p>{body}</p><p><i>{tail}</i></p>"


def utc_from_unix(value: int | float | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromtimestamp(float(value), UTC)
