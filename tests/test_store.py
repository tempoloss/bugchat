import sqlite3

import pytest

from bugbot.store import Store


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "state.db")
    yield db
    db.close()


def link(
    db, message_id, *, issue_id="uuid-1", sequence_id=7, group="backlog", chat_id=-100, source_text="", author_id=0
):
    return db.link_issue(
        chat_id=chat_id,
        message_id=message_id,
        issue_id=issue_id,
        sequence_id=sequence_id,
        project_id="proj",
        url=f"https://plane/{sequence_id}",
        state_group=group,
        source_text=source_text,
        author_id=author_id,
        author_name=f"user{author_id}",
    )


def test_offset_roundtrip_survives_reopen(tmp_path):
    path = tmp_path / "state.db"
    first = Store(path)
    assert first.offset == 0
    first.offset = 512
    first.close()

    second = Store(path)
    assert second.offset == 512
    second.close()


def test_issue_lookup_by_source_and_bot_message(store):
    link(store, 5)
    store.set_bot_message(-100, 5, 6)

    by_source = store.issue_for_message(-100, 5)
    assert by_source is not None
    assert (by_source.issue_id, by_source.sequence_id, by_source.cancelled) == ("uuid-1", 7, False)

    by_bot = store.issue_by_bot_message(-100, 6)
    assert by_bot is not None and by_bot.issue_id == "uuid-1"

    assert store.issue_for_message(-100, 999) is None
    assert store.issue_by_bot_message(-100, 999) is None


def test_album_parts_share_one_issue(store):
    """Каждая часть альбома указывает на ту же задачу — ответ на любую станет комментарием."""
    for message_id in (10, 11, 12):
        link(store, message_id)
    assert {store.issue_for_message(-100, mid).issue_id for mid in (10, 11, 12)} == {"uuid-1"}


def test_cancel_marks_every_row_of_the_same_issue(store):
    for message_id in (5, 6):
        link(store, message_id, issue_id="u")
    link(store, 7, issue_id="other", sequence_id=2)

    store.mark_cancelled("u")

    assert store.issue_for_message(-100, 5).cancelled is True
    assert store.issue_for_message(-100, 6).cancelled is True
    assert store.issue_for_message(-100, 7).cancelled is False


def test_processed_is_idempotent(store):
    assert not store.is_processed(1)
    store.mark_processed(1)
    store.mark_processed(1)
    assert store.is_processed(1)
    assert not store.is_processed(2)


def test_album_becomes_due_only_after_debounce(store):
    store.add_album_part("mg1", -100, 1, {"message_id": 1, "caption": "падает"})
    store.add_album_part("mg1", -100, 2, {"message_id": 2})

    assert store.due_albums(3600.0) == []
    assert store.due_albums(0.0) == ["mg1"]

    parts = store.pop_album("mg1")
    assert [p["message_id"] for p in parts] == [1, 2]
    assert parts[0]["caption"] == "падает"
    assert store.due_albums(0.0) == []


def test_album_part_insert_is_idempotent(store):
    store.add_album_part("mg1", -100, 1, {"message_id": 1, "caption": "первый"})
    store.add_album_part("mg1", -100, 1, {"message_id": 1, "caption": "дубль"})
    parts = store.pop_album("mg1")
    assert len(parts) == 1
    assert parts[0]["caption"] == "первый"


def test_dead_letter_records_failure(store):
    store.dead_letter(42, {"update_id": 42}, "PlaneError('500')")
    row = store._db.execute("SELECT update_id, error FROM deadletter").fetchone()
    assert row["update_id"] == 42
    assert "PlaneError" in row["error"]


# ---- слежение за закрытием ------------------------------------------------
def test_tracked_issues_returns_anchor_row_per_issue(store):
    """Альбом = три строки, но отвечать надо один раз и в первое сообщение."""
    for message_id in (10, 11, 12):
        link(store, message_id, issue_id="album")
    link(store, 20, issue_id="single", sequence_id=8)

    tracked = {ref.issue_id: ref for ref in store.tracked_issues()}
    assert set(tracked) == {"album", "single"}
    assert tracked["album"].message_id == 10
    assert tracked["single"].message_id == 20


def test_state_group_is_seeded_and_updated(store):
    link(store, 5, group="backlog")
    assert store.issue_for_message(-100, 5).last_state_group == "backlog"

    store.set_state_group("uuid-1", "completed")
    assert store.issue_for_message(-100, 5).last_state_group == "completed"


def test_state_group_update_covers_all_album_rows(store):
    for message_id in (10, 11):
        link(store, message_id, issue_id="album")
    store.set_state_group("album", "completed")
    assert store.issue_for_message(-100, 11).last_state_group == "completed"


def test_old_database_gains_the_new_column(tmp_path):
    """state.db с боевой машины уже содержит задачи — обновление не должно его ломать."""
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE issues (
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, issue_id TEXT NOT NULL,
            sequence_id INTEGER NOT NULL, project_id TEXT NOT NULL, url TEXT NOT NULL,
            bot_message_id INTEGER, cancelled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        );
        INSERT INTO issues VALUES (-100, 8, 'old-uuid', 5, 'proj', 'https://plane/5', NULL, 0, 0.0);
        """
    )
    legacy.commit()
    legacy.close()

    upgraded = Store(path)
    try:
        ref = upgraded.issue_for_message(-100, 8)
        assert ref is not None and ref.sequence_id == 5
        assert ref.last_state_group is None  # неизвестно → первый опрос просто запомнит текущее
        upgraded.set_state_group("old-uuid", "started")
        assert upgraded.issue_for_message(-100, 8).last_state_group == "started"
    finally:
        upgraded.close()


def test_source_text_is_kept_for_closure_comparison(store):
    link(store, 5, source_text="не грузится отчёт")
    assert store.issue_for_message(-100, 5).source_text == "не грузится отчёт"


def test_issue_by_id_returns_the_anchor_row(store):
    for message_id in (11, 10, 12):
        link(store, message_id, issue_id="album")
    ref = store.issue_by_id("album")
    assert ref is not None and ref.message_id == 10
    assert store.issue_by_id("нет такой") is None


def test_last_open_issue_skips_cancelled(store):
    link(store, 1, issue_id="first", sequence_id=1)
    link(store, 2, issue_id="second", sequence_id=2)
    store.mark_cancelled("second")

    ref = store.last_open_issue(-100)
    assert ref is not None and ref.issue_id == "first"

    store.mark_cancelled("first")
    assert store.last_open_issue(-100) is None


def test_last_open_issue_is_per_chat(store):
    link(store, 1, issue_id="ours", chat_id=-100)
    link(store, 1, issue_id="theirs", chat_id=-200)
    assert store.last_open_issue(-200).issue_id == "theirs"


def test_bot_comments_are_remembered_per_issue(store):
    store.remember_bot_comment("issue-a", "c1")
    store.remember_bot_comment("issue-a", "c1")
    store.remember_bot_comment("issue-a", "c2")
    store.remember_bot_comment("issue-b", "c3")

    assert store.bot_comment_ids("issue-a") == {"c1", "c2"}
    assert store.bot_comment_ids("issue-b") == {"c3"}
    assert store.bot_comment_ids("issue-c") == set()


def test_relinking_a_message_resets_tracking(store):
    """Одно сообщение перепривязали к новой задаче — старый текст и состояние не должны прилипнуть."""
    link(store, 8, issue_id="old", sequence_id=5, group="completed", source_text="старая жалоба")
    store.set_bot_message(-100, 8, 99)
    store.mark_cancelled("old")

    link(store, 8, issue_id="new", sequence_id=17, group="backlog", source_text="новая жалоба")

    ref = store.issue_for_message(-100, 8)
    assert ref.issue_id == "new"
    assert ref.source_text == "новая жалоба"
    assert ref.last_state_group == "backlog"
    assert ref.cancelled is False
    assert ref.bot_message_id is None


# ---- серии сообщений и журнал чата ----------------------------------------
def test_burst_waits_for_silence_from_the_same_author(store):
    store.add_burst_part(-100, 7, 1, {"message_id": 1, "text": "у меня проблема"})
    store.add_burst_part(-100, 7, 2, {"message_id": 2, "text": "при экспорте"})

    assert store.due_bursts(3600.0) == []
    assert store.due_bursts(0.0) == [(-100, 7)]

    parts = store.pop_burst(-100, 7)
    assert [p["text"] for p in parts] == ["у меня проблема", "при экспорте"]
    assert store.due_bursts(0.0) == []


def test_bursts_of_different_authors_do_not_merge(store):
    store.add_burst_part(-100, 7, 1, {"message_id": 1, "text": "мой баг"})
    store.add_burst_part(-100, 8, 2, {"message_id": 2, "text": "чужой баг"})

    assert sorted(store.due_bursts(0.0)) == [(-100, 7), (-100, 8)]
    assert [p["text"] for p in store.pop_burst(-100, 7)] == ["мой баг"]
    assert [p["text"] for p in store.pop_burst(-100, 8)] == ["чужой баг"]


def test_dropping_burst_parts_prevents_a_duplicate_task(store):
    """`/task` забрал сообщения — через полминуты серия не должна завести их второй раз."""
    for message_id in (1, 2, 3):
        store.add_burst_part(-100, 7, message_id, {"message_id": message_id})
    store.drop_burst_parts(-100, [1, 2])

    assert [p["message_id"] for p in store.pop_burst(-100, 7)] == [3]


def test_message_log_returns_the_message_and_its_neighbours(store):
    for message_id in range(10, 16):
        store.log_message(-100, message_id, 7, {"message_id": message_id, "text": f"m{message_id}"})

    picked = store.messages_from(-100, 11, 3)
    assert [p["message_id"] for p in picked] == [11, 12, 13]


def test_message_log_stops_at_what_it_has(store):
    store.log_message(-100, 10, 7, {"message_id": 10})
    assert len(store.messages_from(-100, 10, 4)) == 1
    assert store.messages_from(-100, 999, 4) == []


def test_message_log_is_scoped_to_the_chat(store):
    store.log_message(-100, 10, 7, {"message_id": 10, "text": "наш"})
    store.log_message(-200, 10, 7, {"message_id": 10, "text": "чужой"})
    assert [p["text"] for p in store.messages_from(-100, 10, 5)] == ["наш"]


def test_pruning_drops_only_old_messages(store):
    store.log_message(-100, 10, 7, {"message_id": 10})
    assert store.prune_messages(3600.0) == 0
    assert store.prune_messages(0.0) == 1
    assert store.messages_from(-100, 10, 5) == []


# ---- личный список --------------------------------------------------------
def test_vault_lists_only_my_issues_newest_first(store):
    link(store, 1, issue_id="mine-old", sequence_id=1, author_id=777)
    link(store, 2, issue_id="theirs", sequence_id=2, author_id=888)
    link(store, 3, issue_id="mine-new", sequence_id=3, author_id=777)

    mine = store.issues_of_author(777)
    assert [ref.issue_id for ref in mine] == ["mine-new", "mine-old"]
    assert store.issues_of_author(999) == []


def test_vault_shows_one_row_per_issue(store):
    """Альбом = несколько строк, но в списке задача должна быть одна."""
    for message_id in (10, 11, 12):
        link(store, message_id, issue_id="album", author_id=777)
    assert len(store.issues_of_author(777)) == 1
