"""Перевод HTML описания Plane в подмножество, которое переваривает Telegram.

Редактор Plane отдаёт `<p>`, `<h2>`, `<ul>`, `<img>`, `<mention-component>` и прочее,
а Bot API принимает только `b/i/u/s/a/code/pre/blockquote`. Всё остальное надо
разложить в текст, иначе `sendMessage` вернёт 400 «can't parse entities».
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
import re

TELEGRAM_LIMIT = 4096

_INLINE = {
    "b": "b",
    "strong": "b",
    "i": "i",
    "em": "i",
    "u": "u",
    "ins": "u",
    "s": "s",
    "strike": "s",
    "del": "s",
    "code": "code",
    "pre": "pre",
    "blockquote": "blockquote",
}
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BLOCKS = frozenset({"p", "div", "hr", "table", "tr", "td", "th", "section", "article", "figure", "figcaption"})
_DROPPED = frozenset({"script", "style", "head"})

_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+\n")
_META_LINE_RE = re.compile(r"\s*(?:<b>)?(?:Автор|Чат|Время|Вложений|Сообщение):", re.IGNORECASE)
"""Строки подписи бота в описании задачи — в чат их возвращать незачем."""


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._open: list[str] = []
        """Открытые теги в порядке вложенности; пустая строка = тег проглочен."""
        self._skip = 0
        self._pre = 0
        self._lists: list[int | None] = []
        """None — маркированный список, число — счётчик нумерованного."""

    # ---- вывод ----
    def _newline(self) -> None:
        if self._out and not self._out[-1].endswith("\n"):
            self._out.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROPPED:
            self._skip += 1
            return
        if self._skip:
            return

        if tag == "br":
            self._out.append("\n")
            return
        if tag == "img":
            self._out.append("[изображение]")
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            # только http(s): javascript:/data: в чат не пускаем
            if href.startswith(("http://", "https://")):
                self._out.append(f'<a href="{html.escape(href, quote=True)}">')
                self._open.append("a")
            else:
                self._open.append("")
            return
        if tag in ("ul", "ol"):
            self._lists.append(0 if tag == "ol" else None)
            self._newline()
            return
        if tag == "li":
            self._newline()
            if self._lists and self._lists[-1] is not None:
                self._lists[-1] += 1
                self._out.append(f"{self._lists[-1]}. ")
            else:
                self._out.append("• ")
            return
        if tag in _HEADINGS:
            self._newline()
            self._out.append("<b>")
            self._open.append("b")
            return

        mapped = _INLINE.get(tag)
        if mapped:
            if mapped == "pre":
                self._pre += 1
            self._out.append(f"<{mapped}>")
            self._open.append(mapped)
            return
        if tag in _BLOCKS:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return

        if tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            self._newline()
            return
        if tag == "li":
            return
        if tag == "a" or tag in _HEADINGS or tag in _INLINE:
            if self._open:
                opened = self._open.pop()
                if opened:
                    self._out.append(f"</{opened}>")
                if opened == "pre":
                    self._pre = max(0, self._pre - 1)
            return
        if tag in _BLOCKS:
            self._newline()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "img", "hr"):
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if not self._pre:
            # переносы внутри HTML — форматирование разметки, а не текста
            data = data.replace("\n", " ").replace("\t", " ")
            if not data.strip() and self._out and self._out[-1].endswith("\n"):
                return
        self._out.append(html.escape(data))

    def result(self) -> str:
        while self._open:
            opened = self._open.pop()
            if opened:
                self._out.append(f"</{opened}>")
        text = "".join(self._out)
        text = _TRAILING_SPACE_RE.sub("\n", text)
        return _MULTI_NEWLINE_RE.sub("\n\n", text).strip()


def _drop_meta_lines(text: str) -> str:
    """Убирает строки мета-блока, который бот сам приписал при заведении задачи.

    Работаем по тексту, а не по тегам: редактор Plane при первом же сохранении
    переписывает разметку (`<hr>` → `<div data-type="horizontalRule">`, `<b>` →
    `<strong>`, к `<p>` прилипают классы), поэтому любая привязка к форме тегов
    разваливается после первой правки человеком. Строки-подписи стабильны.

    Всё остальное остаётся — включая решение, дописанное разработчиком ниже.
    """
    kept = [line for line in text.splitlines() if not _META_LINE_RE.match(line)]
    return _MULTI_NEWLINE_RE.sub("\n\n", "\n".join(kept)).strip()


def to_telegram_html(description_html: str, *, limit: int = TELEGRAM_LIMIT) -> str:
    converter = _Converter()
    converter.feed(description_html or "")
    converter.close()
    text = _drop_meta_lines(converter.result())
    if len(text) <= limit:
        return text
    # Обрезаем по границе строки, чтобы не оборвать тег пополам.
    cut = text[: limit - 1]
    newline = cut.rfind("\n")
    if newline > limit // 2:
        cut = cut[:newline]
    return _close_open_tags(cut)


def _close_open_tags(text: str) -> str:
    """Дозакрывает теги, которые могли остаться открытыми после обрезки."""
    reopened = re.findall(r"<(/?)(b|i|u|s|a|code|pre|blockquote)\b[^>]*>", text)
    stack: list[str] = []
    for closing, tag in reopened:
        if closing:
            if stack and stack[-1] == tag:
                stack.pop()
        else:
            stack.append(tag)
    return text + "".join(f"</{tag}>" for tag in reversed(stack)) + "…"


_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|\w+);")


def to_plain_text(telegram_html: str) -> str:
    """Аварийный вариант: снять всю разметку, оставить читаемый текст."""
    return html.unescape(_TAG_RE.sub("", telegram_html)).strip()


def strip_known_prefix(rendered: str, source_text: str) -> str:
    """Отрезает исходную жалобу из начала описания, возвращая только дописанное.

    Описание задачи начинается с текста сообщения — пересказывать его в ответе
    на это же сообщение бессмысленно. Интересно ровно то, что добавили сверху.
    Пустой результат означает «ничего нового не написали».
    """
    body = rendered.strip()
    source = (source_text or "").strip()
    if not body:
        return ""
    if not source:
        return body  # задачи из старых версий — источник не сохраняли, отдаём как есть

    plain = to_plain_text(body)
    if not plain.startswith(source):
        return body
    if plain[len(source) :].strip() == "":
        return ""
    # Режем по разметке, а не по plain-тексту: иначе `<b>Решение:</b>` вернулось бы голым.
    return _tail_after_plain(body, len(source)).strip()


def _tail_after_plain(body: str, plain_chars: int) -> str:
    """Хвост размеченной строки после первых `plain_chars` символов видимого текста."""
    seen = 0
    index = 0
    while index < len(body) and seen < plain_chars:
        char = body[index]
        if char == "<":
            close = body.find(">", index)
            index = len(body) if close == -1 else close + 1
            continue
        if char == "&" and (entity := _ENTITY_RE.match(body, index)):
            index = entity.end()
        else:
            index += 1
        seen += 1
    return body[index:]
