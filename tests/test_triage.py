from datetime import UTC, datetime

import pytest

from bugbot import app, triage

WHEN = datetime(2026, 8, 3, 12, 30, tzinfo=UTC)
MIN_LEN = 120


def actionable(text: str, *, has_media: bool = False) -> bool:
    return triage.is_actionable(text, has_media=has_media, min_text_len=MIN_LEN)


@pytest.mark.parametrize(
    "text",
    [
        "в поиске по ИИН вылезает 500",
        "Отчёт не выгружается, крутится вечно",
        "кнопка сохранить не работает",
        "упал импорт выписок",
        "Traceback (most recent call last): ...",
        "карточка física показывает некорректный баланс",
    ],
)
def test_complaints_become_issues(text):
    assert actionable(text)


@pytest.mark.parametrize("text", ["ок", "спасибо!", "+", "когда посмотрите?", "доброе утро", "я в отпуске до среды"])
def test_chatter_is_ignored(text):
    assert not actionable(text)


def test_media_alone_is_enough():
    assert actionable("", has_media=True)
    assert actionable("вот", has_media=True)


def test_long_message_without_keywords_still_files():
    assert actionable("а" * MIN_LEN)
    assert not actionable("а" * (MIN_LEN - 1))


def test_fix_report_is_not_a_bug_even_with_screenshot():
    assert not actionable("уже работает, спасибо", has_media=True)
    assert not actionable("пофиксил на проде")


def test_fix_report_with_new_complaint_is_a_bug():
    assert actionable("пофиксил экспорт, но теперь не открывается карточка")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("СРОЧНО прод лежит", "urgent"),
        ("критично: ничего не работает", "urgent"),
        ("не сохраняется черновик", "high"),
        ("надо бы поменять цвет кнопки на синий, чтобы совпадал с макетом дизайнера", "none"),
    ],
)
def test_priority(text, expected):
    assert triage.priority_for(text) == expected


def test_title_strips_openers_and_shouting():
    assert triage.make_title("ребят, кароче не грузится список дел!!!", author="@t", has_media=False, when=WHEN) == (
        "Не грузится список дел"
    )
    assert triage.make_title("@anuar смотри тут ошибка вылезает", author="@t", has_media=False, when=WHEN) == (
        "Тут ошибка вылезает"
    )


def test_title_takes_first_line_and_strips_noise():
    text = "#баг  — Не грузится список дел\n\nподробности ниже"
    assert triage.make_title(text, author="Иван", has_media=False, when=WHEN) == "Не грузится список дел"


def test_title_is_capitalized():
    assert triage.make_title("падает экспорт в эксель", author="@t", has_media=False, when=WHEN)[0] == "П"


def test_title_drops_emoji():
    title = triage.make_title("😡 всё сломалось на проде", author="@t", has_media=False, when=WHEN)
    assert title == "Всё сломалось на проде"


def test_title_cuts_at_first_sentence():
    text = "при экспорте падает 500. воспроизводится каждый раз на большом фильтре, уже третий день"
    assert triage.make_title(text, author="@t", has_media=False, when=WHEN) == "При экспорте падает 500"


def test_short_title_gets_author_and_date_to_stay_distinguishable():
    """Три задачи «не работает» на доске неразличимы — дополняем автором и временем."""
    title = triage.make_title("не работает", author="@tempoloss", has_media=True, when=WHEN)
    assert title.startswith("Не работает — @tempoloss")
    assert "03.08" in title


def test_short_first_line_pulls_the_next_one():
    title = triage.make_title("ошибка\nподробности: 500 на бэке", author="@t", has_media=False, when=WHEN)
    assert title == "Ошибка: подробности: 500 на бэке"


def test_title_is_shortened_on_word_boundary():
    title = triage.make_title("слово " * 40, author="Иван", has_media=False, when=WHEN)
    assert len(title) <= triage.TITLE_LIMIT
    assert title.endswith("…")
    assert not title.endswith(" …")


def test_title_falls_back_for_media_only():
    title = triage.make_title("", author="@vasya", has_media=True, when=WHEN)
    assert "скриншот" in title
    assert "@vasya" in title


def test_description_escapes_user_html():
    body = triage.render_description(
        text="<script>alert(1)</script> & co",
        author="Иван <b>",
        chat_title="Баги",
        when=WHEN,
        link="https://t.me/c/1/2",
    )
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "Иван &lt;b&gt;" in body
    assert 'href="https://t.me/c/1/2"' in body


def test_description_keeps_line_breaks_and_meta():
    body = triage.render_description(
        text="строка1\nстрока2", author="Иван", chat_title="Баги", when=WHEN, link=None, attachments=2
    )
    assert "строка1<br/>строка2" in body
    assert "Вложений:</b> 2" in body
    assert "03.08.2026 12:30" in body


def test_comment_render_mentions_author_and_link():
    body = triage.render_comment(text="ещё повторилось", author="@vasya", when=WHEN, link="https://t.me/c/1/9")
    assert "ещё повторилось" in body
    assert "@vasya" in body
    assert 'href="https://t.me/c/1/9"' in body


def test_author_name_variants():
    assert (
        triage.author_name({"first_name": "Иван", "last_name": "Петров", "username": "ivan"}) == "Иван Петров (@ivan)"
    )
    assert triage.author_name({"username": "ivan"}) == "@ivan"
    assert triage.author_name({"first_name": "Иван", "id": 7}) == "Иван (id 7)"
    assert triage.author_name(None) == "неизвестный автор"


# ---- разбор команды /task ---------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/task", 1),
        ("/task 4", 4),
        ("/task@examplebot 3", 3),
        ("/task 0", 1),
        ("/task -2", 1),
        ("/task много", 1),
        ("/task 999", app.MAX_TASK_SPAN),
    ],
)
def test_task_count_parsing(text, expected):
    assert app._task_count(text) == expected


# ---- заголовок из склеенной серии сообщений --------------------------------
def test_series_title_collects_the_whole_thought():
    """Мысль разбита на три сообщения — заголовок не должен обрываться на первом."""
    text = "тут беда с реестром\nпри выгрузке за квартал\nвылетает 500 и ничего не скачивается"
    title = triage.make_title(text, author="@t", has_media=False, when=WHEN, parts=3)
    assert title == "Тут беда с реестром при выгрузке за квартал"


def test_series_title_stops_once_it_is_informative():
    text = "не выгружается реестр контрагентов за третий квартал\nещё детали\nи ещё"
    title = triage.make_title(text, author="@t", has_media=False, when=WHEN, parts=3)
    assert title == "Не выгружается реестр контрагентов за третий квартал"


def test_single_message_keeps_only_its_first_line():
    """Одно сообщение: первая строка — это уже саммари, добирать абзацы не надо."""
    text = "Не грузится список дел\n\nподробности ниже, воспроизводится у всех"
    assert triage.make_title(text, author="@t", has_media=False, when=WHEN, parts=1) == "Не грузится список дел"


# --- заявки на функционал -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "добавьте кнопку экспорта в реестр",
        "можно ли добавить фильтр по дате",
        "хотелось бы видеть сумму итогом",
        "не хватает поиска по ИИН",
        "предлагаю вынести это в отдельную вкладку",
        "было бы удобно сортировать по дате",
        "нужна возможность выгрузить в excel",
        "запилите тёмную тему пж",
    ],
)
def test_feature_requests_are_filed(text):
    assert actionable(text) is True
    assert triage.kind_for(text) == triage.FEATURE


@pytest.mark.parametrize(
    "text",
    [
        # Прошедшее время — это отчёт о сделанном, а не заявка. Стем `добав`
        # целиком поймал бы и его.
        "добавили новую колонку в отчёт",
        "добавил индекс, стало быстрее",
    ],
)
def test_past_tense_is_not_a_request(text):
    assert triage.kind_for(text) == triage.BUG


def test_complaint_beats_request_in_one_message():
    # «Добавьте фильтр, а то выгрузка падает» — это про падение. Заявка подождёт,
    # а баг помечать низким приоритетом было бы прямо вредно.
    text = "добавьте фильтр, а то выгрузка падает с 500"
    assert triage.kind_for(text) == triage.BUG
    assert triage.priority_for(text) == "high"


def test_a_request_is_never_urgent():
    # Автор заявки всегда считает её важной. Дай ему выставлять срочность словом —
    # и через месяц вся доска urgent.
    assert triage.priority_for("СРОЧНО добавьте выгрузку в pdf") == "low"


def test_short_request_still_gets_filed():
    # Заявка обычно короче порога min_text_len и без баг-лексики: до появления
    # словаря заявок такое сообщение молча терялось.
    text = "добавьте сортировку"
    assert len(text) < MIN_LEN
    assert actionable(text) is True
    assert triage.kind_for(text) == triage.FEATURE
