# Pulse checker backend

The backend is a compatibility/fallback service for Pulse 0.7.0. The Android app's
normal collection path no longer depends on Render or any third-party scraper:

Android APK -> Instagram directly from the phone -> validated local Snapshot.

The existing Render service remains available at the same API contract for diagnostics
and fallback use:

client -> Render pulse-checker -> our direct Instagram collector -> validated Snapshot.

There are no HikerAPI, Apify, public Actor, paid scraper, or provider-token calls in
`checker_backend/main.py`.

## Required Render environment

- `IG_SESSION_JSON`: checker-session cookies used only by the server-side direct fallback.

Keep the session only in Render environment variables. Never commit it to GitHub.

Optional:

- `COLLECTION_TIMEOUT` (default 180 seconds, clamped to 30-210)
- direct collector pacing/retry variables already defined in `direct_instagram.py`

The server-side fallback can still be rate-limited or see fewer relationship rows from a
cloud IP. That does not affect the Android 0.7.0 normal path, which collects through the
phone's own Instagram session and network.

## Completeness model

The direct collector:

1. resolves the exact target profile ID and followers/following counts;
2. fetches relationship pages from Instagram directly;
3. merges the supported Instagram relationship hosts by stable numeric user ID;
4. rejects pagination loops and incomplete lists;
5. re-fetches exact profile counts after collection;
6. returns `complete: true` only when both list sizes still match the counts.

A partial list is never returned as a valid snapshot.

## API compatibility

`POST /v1/collect` keeps the existing response fields:

- `account`
- `captured_at`
- `followers_count`
- `following_count`
- `followers`
- `following`
- `complete`
- `source`

Each member remains `{id, username}`.

`GET /health` reports `engine: pulse-direct-instagram`,
`third_party_scraper: false`, and whether the Render checker session is configured.

## Render

Keep the existing service:

- service: `pulse-checker`
- branch: `main`
- build: `python -m checker_backend.build`
- start: `uvicorn checker_backend.main:app --host 0.0.0.0 --port $PORT --workers 1`
- health check: `/health`

Do not add a third-party scraper token back to the service. The old Hiker backup branch
is unrelated to the 0.7.0 direct path.

Run checks with:

```bash
python -m unittest discover -s checker_backend/tests -v
```
