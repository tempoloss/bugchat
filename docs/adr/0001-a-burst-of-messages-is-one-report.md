# 1. A burst of messages from one author is one report

Status: accepted

Introduced in `4dd9201` (2026-08-03), pinned by the burst tests in
`tests/test_commands.py`. Written down as an ADR on 2026-08-04, when the decisions
that until then lived only in commit messages were filed here.

## Context

A bug is rarely one message. What actually arrives is this:

```
10:26  тут беда с реестром
10:26  при выгрузке за квартал
10:27  вылетает 500
```

That is one problem, typed by a person thinking out loud. The first implementation
triaged each update as it arrived, so the chat above produced three tasks: «Тут беда
с реестром», «При выгрузке за квартал», «Вылетает 500». None of the three says
enough to act on, and a board holding all three is worse than a board holding
nothing, because now somebody has to reconcile them.

Screenshots made it worse. A screenshot usually follows the sentence it belongs to,
so the image landed on a different task from the description of what it shows.

## Decision

Do not triage per message. Collect messages per author, wait for
`BUGBOT_BURST_SECONDS` of silence from that author, then triage the whole series at
once. Screenshots from the same series attach to the same task.

The buffer is a table rather than memory (`burst_parts` in `store.py`), so a restart
mid-burst does not lose the half that already arrived.

## Consequences

**The reply is late on purpose.** The bot answers about twenty seconds after the
last message instead of instantly. That is the price of the merge and it is stated
in the README rather than left as a surprise. `BUGBOT_BURST_SECONDS = 0` restores
per-message behaviour for anyone who prefers the old trade.

**Two authors complaining at once stay separate.** The buffer is keyed by author, so
interleaved reports do not merge into one task.

**A slow typist can still split a report.** Somebody who pauses for a minute
mid-thought gets two tasks. The window is a guess about human rhythm, not a
guarantee, and `/task N` exists to merge messages by hand when the guess is wrong.
