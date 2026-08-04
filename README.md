# bugchat — a Telegram bug chat becomes a Plane board

Listens to a chat where people complain, ignores the small talk, and files what is
left into self-hosted [Plane](https://plane.so). Replies in the thread become
comments, screenshots become attachments, and when the task closes the bot answers
the person who reported it.

[Русская версия](README.ru.md)

## Why this exists

Plane has a web UI and a mobile app, so a bot that only creates issues from
commands is a worse version of both. That is not what this does.

The thing a task tracker cannot do is be present in the room where the complaint
happens. People report problems in chat, in fragments, without a title, and then
carry on talking. Nobody transcribes that onto a board afterwards, so the report
is lost — not ignored, lost. This bot lives in that room.

## What it does

| In the chat | In Plane |
|---|---|
| a complaint («не открывается», «500», «падает») or a screenshot | a task in `Backlog`, labelled `telegram` + `bug` |
| a request («добавьте», «хотелось бы», «не хватает») | a task labelled `feature`, priority `low` |
| «срочно», «критично», «прод лежит» | priority `urgent`; an ordinary complaint gets `high` |
| screenshots | **embedded in the description**; files and video become attachments |
| an album of several screenshots | **one** task holding all of them |
| a reply to a message that already has a task | a comment on that task |
| an edit to the original message | the description is updated |
| the «🚫 Не баг» button, `/skip`, or a 👎 reaction | the task moves to `Cancelled` |
| «ок», «спасибо», «когда посмотрите?» | nothing |

Each filed task gets a reply in the chat — `🐞 OPS-12 · Backlog · high`, the title,
and a one-tap cancel button. A request gets `💡` instead, so the author sees how the
bot understood them and can correct it immediately rather than a week later on the
board.

## The three decisions that matter

Everything above is plumbing. These are the parts worth stealing.

### One report is one task, not three fragments

A bug is rarely one message. «тут беда с реестром», then «при выгрузке за квартал»,
then «вылетает 500» is one problem typed by a human thinking out loud. Handled
message by message it becomes three useless stubs.

So the bot does not triage per message. It collects them per author and waits for
`BUGBOT_BURST_SECONDS` of silence from that person, then triages the whole series at
once. Screenshots from the same series land in the same task.

**The cost is stated rather than hidden:** the reply arrives about twenty seconds
later instead of instantly. That is the trade, and `0` restores per-message
behaviour if you want it.

### Titles are normalised, and short ones are made unique

The first line of a chat message is not a title. Openers, filler and shouting come
off, emoji and hashtags go, the text is cut at the first sentence end, and the
result starts with a capital.

| In the chat | On the board |
|---|---|
| `ребят, кароче не грузится список дел!!!` | Не грузится список дел |
| `не работает` + screenshot | Не работает — @someone, 03.08 10:26 |
| `при экспорте падает 500. воспроизводится каждый раз` | При экспорте падает 500 |

The second row is the interesting one. A title too short to explain anything gets
the author and the time appended, because three tasks called «не работает» are
indistinguishable on a board — and that is a problem you only meet after the first
week, not while writing the parser.

### A request is never urgent

A complaint has vocabulary. A request does not: «добавьте кнопку экспорта» carries
no bug words and is too short to trip a length threshold, so it used to vanish
between the filter and the board.

A second dictionary now recognises requests, and two rules come with it.

**A complaint beats a request in the same message.** «Добавьте фильтр, а то выгрузка
падает» is about the crash. Filing that as a low-priority wish would be actively
wrong.

**A request is never urgent**, not even «СРОЧНО добавьте выгрузку в pdf». The author
of a request always thinks it is important, and if a word can set urgency then the
whole board is urgent within a month. A wish is not an incident.

The request dictionary deliberately excludes past tense: «добавили колонку» reports
work already done. A missed request can be filed by hand with `/task`; a false one
quietly clutters the board, which is worse.

## Setup

**1. A Telegram bot.** Token from [@BotFather](https://t.me/BotFather).

**Add it to the chat as an administrator.** This is not optional: privacy mode hides
ordinary messages from bots, and reaction updates (👎 to cancel) are only delivered
to admins. The alternative for messages, but not for reactions, is `/setprivacy` →
`Disable`.

Find the chat id by running the bot and sending `/chatid`.

**2. Plane credentials.** A normal account with access to the target project.

There is no API token, because [Plane's self-hosted builds can have them
disabled](#session-auth) — the bot signs in with a session, like a browser. Labels
are created automatically.

**3. Configure and run:**

```bash
cp .env.example .env    # then fill it in
uv sync --python 3.12
uv run python -m bugbot
```

`PLANE_BASE`, `PLANE_WORKSPACE` and `BUGBOT_PLANE_PROJECT` have no defaults on
purpose: a fallback host means signing in somewhere other than where you think, so
the bot refuses to start instead.

## Session auth

<a id="session-auth"></a>

Plane's documented automation path is `/api/v1/` with an `X-API-Key`. In self-hosted
builds that endpoint can be missing entirely — `/api/workspaces/{ws}/api-tokens/`
answers 404 and there is no key to issue. The bot therefore does what a browser
does:

1. `GET /auth/get-csrf-token/` for the token and the `csrftoken` cookie.
2. `POST /auth/sign-in/` with `csrfmiddlewaretoken`, `email`, `password`. The
   `Referer` and `Origin` headers are **mandatory** — without them the request fails
   CSRF validation with no useful message. Success is **HTTP 302**, not 200, and it
   sets a `session-id` cookie good for seven days.
3. Internal `/api/...` with that cookie. Writes also need `X-CSRFTOKEN`.

If you are automating a self-hosted Plane and hit the same wall, `bugbot/plane.py`
is the part to read.

## Deployment

Docker Compose, state in a named volume, `.env` on the host only:

```bash
docker compose up -d --build
docker compose logs -f bugbot
```

`scripts/deploy.sh` rebuilds and then waits for the readiness line in the log,
failing if it does not appear within a minute. A failed Plane login would otherwise
look exactly like a successful deploy.

**Run exactly one instance per bot token.** Two processes polling `getUpdates` with
the same token fight, and Telegram answers `409 Conflict` to the loser. Stop a local
copy before shipping.

## Commands

| Command | What it does |
|---|---|
| `/my` | every task you filed, with current status from Plane |
| `/task` 🔒 | as a reply — file this message, bypassing the filter |
| `/task 4` 🔒 | as a reply to the first of four — **one** task from all four |
| `/skip` | as a reply, cancel that task; without one, the last filed here |
| `/chatid` | prints the chat id and whether it is watched |
| `/ping` | alive, and where tasks go |
| `/help` | short help for colleagues |

🔒 — chat admins only. `/task` bypasses the filter, so it is the easiest way to bury
a board. Rights are checked on every call, so a demoted admin loses it immediately
without a restart. The command menu is registered per role: a command that would
refuse on rights is simply not offered.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `BUGBOT_BURST_SECONDS` | 20 | silence to wait before treating an author's series as finished |
| `BUGBOT_MIN_TEXT_LEN` | 120 | a long message with no bug words still files a task |
| `BUGBOT_ALBUM_DEBOUNCE` | 2.0 | seconds to wait for the rest of an album |
| `BUGBOT_MESSAGE_LOG_DAYS` | 7 | how long to remember messages, needed for `/task N` |
| `BUGBOT_POLL_TIMEOUT` | 25 | `getUpdates` long-poll seconds |
| `BUGBOT_MAX_ATTACHMENT_MB` | 20 | the Bot API will not serve more anyway |
| `BUGBOT_CLOSE_POLL_SECONDS` | 60 | how often to ask Plane about closures; `0` disables replies |
| `BUGBOT_PLANE_STATE` | `Backlog` | state for new tasks |
| `BUGBOT_PLANE_CANCEL_STATE` | `Cancelled` | where a cancelled task goes |
| `BUGBOT_PLANE_LABELS` | `telegram` | labels on every task from the bot |
| `BUGBOT_PLANE_BUG_LABEL` | `bug` | complaint label, in addition to the common ones |
| `BUGBOT_PLANE_FEATURE_LABEL` | `feature` | request label |
| `PLANE_INSECURE` | unset | `1` skips TLS verification; exposes credentials, use a CA bundle instead |

Kind labels are separate from the common set, and the common set is filtered against
them, so a config still listing `telegram,bug` cannot produce a request labelled as
both. That divergence is only visible by eye on the board.

The three dictionaries — complaints, requests, and stop words like «починил» — live
in `bugbot/triage.py` and are covered by tests. They are Russian, because the chat
this was built for is. Edit the regexes and run `pytest`.

## Tests

```bash
uv run pytest -q
```

120 tests, no network. The impure half is a thin shell around a pure one: triage,
title normalisation and Plane-HTML translation are all pure functions, so every path
is testable without a Telegram or a Plane.

`scripts/live_check.py` runs the real path against a real Plane with Telegram
stubbed: login, labels, a task with a screenshot, a comment, a request with its
label and priority read back from the board, then cancellation. Useful after a
password change or a Plane upgrade.

## Layout

```
bugbot/triage.py      is this a complaint, a request, or noise — pure, all the logic
bugbot/plane.py       Plane over session auth: issues, comments, labels, attachments
bugbot/plane_html.py  Plane HTML -> the little of it Telegram accepts
bugbot/telegram.py    Bot API
bugbot/store.py       SQLite: offsets, message -> task, album and burst buffers
bugbot/app.py         the loops and the decisions between them
scripts/live_check.py the real path against a real Plane, Telegram stubbed
```

## Known limits

Tasks are created by one service account, so Plane shows a single author for
everything. `/my` works from the Telegram sender id instead, and tasks filed before
that was recorded do not appear in it.

A plain group is not a supergroup: messages have no permanent links, so the task
carries no message link, and Telegram **changes the chat id** on upgrade. The bot
picks up the new id and logs which one to write into `.env`.

Deleting `state.db` forgets the message-to-task map. Existing tasks stay, but replies
in old threads stop becoming comments.
