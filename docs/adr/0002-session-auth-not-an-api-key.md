# 2. Sign in with a session, because the documented API key can be absent

Status: accepted

Introduced in `b71152d` (2026-08-03), exercised end to end by
`scripts/live_check.py`. Written down as an ADR on 2026-08-04, when the decisions
that until then lived only in commit messages were filed here.

## Context

Plane documents `/api/v1/` with an `X-API-Key` header as the way to automate it. The
key is issued from the workspace settings.

On the self-hosted build this was written against, that path does not exist:

```
GET /api/workspaces/{workspace}/api-tokens/   ->  404
```

No endpoint, no key, and nothing in the UI to click. The documented integration
surface is simply not compiled into the build. Which leaves either no automation at
all, or the interface the web app itself uses.

## Decision

Authenticate the way the browser does, against the internal `/api/...`:

1. `GET /auth/get-csrf-token/` for the token and the `csrftoken` cookie.
2. `POST /auth/sign-in/` with `csrfmiddlewaretoken`, `email`, `password`.
3. Carry the resulting `session-id` cookie on every request; add `X-CSRFTOKEN` on
   writes.

Three details are not guessable from a failure, so they are commented where they
happen in `plane.py`:

**`Referer` and `Origin` are mandatory.** Without them the POST fails CSRF
validation, and the response says nothing useful about why.

**Success is `302`, not `200`.** `allow_redirects=False` and then checking for a 302
is the check; following the redirect and asserting 200 passes on a failed login.

**The session lasts about seven days.** Long enough that a login bug does not show up
until the following week, which is why `live_check.py` exists.

## Consequences

**The credential is a password, not a scoped token.** It cannot be narrowed to one
project or made read-only, and rotating it means editing `.env` on the host. The
account should be one made for the bot rather than a person's own.

**This is an internal API and may move.** Nothing here is a contract, so a Plane
upgrade can break sign-in. `deploy.sh` waits for the readiness line and fails if it
does not appear, so a broken login surfaces as a failed deploy instead of a bot that
looks alive and files nothing.

**Rejected: no automation until tokens are enabled.** Waiting on somebody else's
build flag is not a plan, and the whole point was to stop losing reports today.
