from bugbot import plane_html

META = (
    "<hr><p><b>Автор:</b> Иван (@ivan)<br><b>Чат:</b> brlaau<br>"
    '<b>Сообщение:</b> <a href="https://t.me/c/1/2">ссылка</a></p>'
)


def convert(source: str) -> str:
    return plane_html.to_telegram_html(source)


def test_paragraphs_become_lines():
    assert convert("<p>первый</p><p>второй</p>") == "первый\nвторой"


def test_supported_inline_tags_are_remapped():
    assert convert("<p><strong>жирный</strong> и <em>курсив</em></p>") == "<b>жирный</b> и <i>курсив</i>"


def test_headings_become_bold():
    assert convert("<h2>Причина</h2><p>гонка</p>") == "<b>Причина</b>\nгонка"


def test_lists_get_markers():
    assert convert("<ul><li>раз</li><li>два</li></ul>") == "• раз\n• два"
    assert convert("<ol><li>раз</li><li>два</li></ol>") == "1. раз\n2. два"


def test_links_survive_but_javascript_is_dropped():
    assert convert('<p><a href="https://plane/1">задача</a></p>') == '<a href="https://plane/1">задача</a>'
    assert convert('<p><a href="javascript:alert(1)">клик</a></p>') == "клик"


def test_unknown_tags_are_stripped_but_text_stays():
    assert convert('<mention-component label="Иван"></mention-component><p>текст</p>') == "текст"
    assert convert("<table><tr><td>ячейка</td></tr></table>") == "ячейка"


def test_images_are_announced():
    assert convert('<p>до<img src="https://x/y.png">после</p>') == "до[изображение]после"


def test_user_text_is_escaped():
    out = convert("<p>если a &lt; b то &amp; всё</p>")
    assert "&lt;" in out and "&amp;" in out
    assert "<script" not in convert("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>")


def test_code_block_keeps_line_breaks():
    assert convert("<pre>line1\nline2</pre>") == "<pre>line1\nline2</pre>"


def test_bot_meta_block_is_dropped():
    assert convert(f"<p>не грузится отчёт</p>{META}") == "не грузится отчёт"


def test_foreign_horizontal_rule_is_kept():
    """Чужой <hr> от разработчика — часть его текста, режем только свой блок."""
    out = convert("<p>причина</p><hr><p>решение: перезапуск</p>")
    assert "причина" in out and "решение: перезапуск" in out


def test_blank_description_gives_empty_string():
    assert convert("") == ""
    assert convert("<p></p>") == ""


def test_long_text_is_truncated_with_closed_tags():
    source = "<p><b>" + "очень длинный текст " * 400 + "</b></p>"
    out = plane_html.to_telegram_html(source, limit=200)
    assert len(out) <= 220
    assert out.endswith("…")
    assert out.count("<b>") == out.count("</b>")


def test_plain_text_fallback_strips_everything():
    assert plane_html.to_plain_text('<b>жирный</b> <a href="https://x">ссылка</a> &amp; текст') == (
        "жирный ссылка & текст"
    )


def test_developer_text_after_meta_block_survives():
    """Разработчик дописывает решение в конец описания — оно обязано доехать в чат."""
    out = convert(f"<p>не грузится отчёт</p>{META}<p>Решение: перевыкатили воркер</p>")
    assert "Решение: перевыкатили воркер" in out
    assert "Автор:" not in out
    assert "не грузится отчёт" in out


def test_description_without_meta_is_untouched():
    assert convert("<p>просто описание</p>") == "просто описание"


# Так описание выглядит ПОСЛЕ того, как его хоть раз сохранили в редакторе Plane:
# <hr> становится div-ом, <b> → <strong>, к <p> прилипают классы и data-id.
PLANE_REWRITTEN = (
    '<p class="editor-paragraph-block" data-id="0a9c">не работает загрузка выгрузок</p>'
    '<div class="py-4 border-strong-1" data-id="c52e" data-type="horizontalRule"><div></div></div>'
    '<p class="editor-paragraph-block" data-id="23f8"><strong>Автор:</strong> tmp (@tempoloss)<br>'
    "<strong>Чат:</strong> brlaau<br><strong>Время:</strong> 03.08.2026 05:06 UTC<br>"
    '<strong>Вложений:</strong> 1<br><strong>Сообщение:</strong> <a target="_blank" '
    'class="text-accent-secondary underline" href="https://t.me/c/4344578237/8" rel="noopener">ссылка</a></p>'
    '<p class="editor-paragraph-block" data-id="9911"><strong>Решение:</strong> поправлено в '
    "<code>export.py</code></p>"
)


def test_meta_is_dropped_even_after_plane_rewrites_the_markup():
    out = convert(PLANE_REWRITTEN)
    assert "Автор:" not in out
    assert "Чат:" not in out
    assert "t.me" not in out
    assert "не работает загрузка выгрузок" in out
    assert "<b>Решение:</b> поправлено в <code>export.py</code>" in out


def test_only_the_added_text_comes_back():
    """Жалоба уже в треде — в ответе нужен только дописанный разработчиком кусок."""
    rendered = "не грузится отчёт\n<b>Решение:</b> перезапустили воркер"
    assert plane_html.strip_known_prefix(rendered, "не грузится отчёт") == "<b>Решение:</b> перезапустили воркер"


def test_untouched_description_yields_nothing():
    assert plane_html.strip_known_prefix("не грузится отчёт", "не грузится отчёт") == ""


def test_rewritten_description_comes_back_whole():
    rendered = "на самом деле падает импорт, а не экспорт"
    assert plane_html.strip_known_prefix(rendered, "не грузится отчёт") == rendered


def test_legacy_issue_without_source_returns_description():
    assert plane_html.strip_known_prefix("что-то сломалось", "") == "что-то сломалось"


def test_empty_description_yields_nothing():
    assert plane_html.strip_known_prefix("", "не грузится отчёт") == ""
