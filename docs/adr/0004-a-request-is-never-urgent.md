# 4. A request is never urgent, whatever the author calls it

Status: accepted

Introduced in `de615e8` (2026-08-04), pinned by `test_a_request_is_never_urgent` and
`test_complaint_beats_request_in_one_message` in `tests/test_triage.py`. Written down
as an ADR the same day.

## Context

The triage recognised complaints by vocabulary: «не работает», «падает», «500». A
request has none of that. «Добавьте кнопку экспорта» carries no bug words and falls
short of `BUGBOT_MIN_TEXT_LEN`, so it was dropped. Silently, with a log line nobody
reads. Work that somebody asked for, lost between the filter and the board.

Filing requests raises two questions that a naive implementation gets wrong.

**What about a message that is both?** «Добавьте фильтр, а то выгрузка падает» matches
the request dictionary and the complaint dictionary at once.

**What about urgency?** People write «срочно» on requests constantly, and the existing
`_URGENT_RE` would have promoted them.

## Decision

A second dictionary recognises requests, and the issue gets the `feature` label
instead of `bug`. Two rules resolve the questions above.

**A complaint beats a request in the same message.** The example above is about the
crash; the filter can wait. Reading the request first would have marked a live 500 as
low priority, which is worse than not filing the request at all.

```python
def kind_for(text: str) -> str:
    if _BUG_RE.search(text):
        return BUG
    return FEATURE if _FEATURE_RE.search(text) else BUG
```

**A request is never urgent**, not even «СРОЧНО добавьте выгрузку в pdf». Priority is
fixed at `low` for the whole kind. The author of a request always believes it is
important. That is why they are asking. So a word that sets urgency is a word every
request will contain, and within a month the board is entirely `urgent` and priority
means nothing.

The request dictionary lists imperative and desiderative forms rather than the stem
`добав`, because «добавили колонку» reports work already done and the stem would file
it as a request.

## Consequences

**The dictionary is narrow on purpose, and misses things.** That is the right side to
err on: a missed request is filed by hand with `/task`, while a false one quietly
clutters the board and nobody knows to remove it.

**Urgency has to be set in Plane.** If a request really does block something, a human
raises it there. The bot has no way to tell the difference and pretending otherwise
would only move the noise.

**Kind labels are separate from the common set, and the common set is filtered against
them.** A config still listing `telegram,bug` cannot produce a request labelled as
both. That divergence is only visible by eye on the board, which is to say not
visible at all.
