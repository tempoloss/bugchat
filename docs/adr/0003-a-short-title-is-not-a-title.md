# 3. A title too short to explain anything is not a title

Status: accepted

Introduced in `449c9c6` (2026-08-03), pinned by the title tests in
`tests/test_triage.py`. Written down as an ADR on 2026-08-04, when the decisions that
until then lived only in commit messages were filed here.

## Context

The obvious title for a task is the first line of the message. It is a bad title.

People open with a greeting, address somebody, shout, and pad. The raw first line
gives `ребят, кароче не грузится список дел!!!`, and a board full of those is
unreadable.

Stripping the noise fixes most of it. It does not fix the case that actually hurts:

```
10:26  не работает          + screenshot
11:40  не работает          + screenshot
14:02  не работает          + screenshot
```

Three real reports about three different things, and after normalisation three tasks
with identical titles. On the board they are indistinguishable, so triage has to open
each one to find out which is which. A duplicate looks exactly like the same report
filed twice.

## Decision

Normalise, then check whether what survived says anything.

Openers, filler, emoji, hashtags and runs of punctuation come off; the text is cut at
the first sentence end and starts with a capital. If the result is shorter than
`_TITLE_MIN_INFORMATIVE`, append the author and the time:

| In the chat | On the board |
|---|---|
| `ребят, кароче не грузится список дел!!!` | Не грузится список дел |
| `не работает` + screenshot | Не работает · @someone, 03.08 10:26 |
| `при экспорте падает 500. воспроизводится каждый раз` | При экспорте падает 500 |

The suffix is not decoration. It is the minimum that makes two reports of «не
работает» tell apart on a list, and author plus time is information the reader
otherwise has to open the task to get.

## Consequences

**Titles are not reversible.** The original text is always in the description, so
nothing is lost, but the title is a summary and should not be parsed back.

**The threshold is a guess.** Fifteen characters is where a Russian phrase usually
stops being a complete thought. It is a constant with a name rather than a magic
number, and it is a constant either way.

**A series contributes more than its first message.** When a burst is merged the
title takes further messages until it reaches `_TITLE_ENOUGH`, because the first
message of a series is often the least specific part of it.
