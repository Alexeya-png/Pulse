# Pulse checker backend

The backend is the no-login fallback for Pulse:

Android APK -> Pulse backend -> direct Instagram collector.

It does not use HikerAPI, Apify, public Actors, or a paid scraping provider.

## Render environment

- `IG_SESSION_JSON`: technical checker-session cookies kept only on Render.
- `COLLECTION_TIMEOUT`: optional timeout.

The checker identity is infrastructure owned by the collector; it is not the Android
user's Instagram session and is never sent to the phone.

## Completeness rules

The profile counters are resolved before collection and rechecked afterwards.

- `followers` is strict. A partial follower list still returns HTTP 409 and is never
  accepted because it could create false unfollow events.
- `following` may be identity-filtered by Instagram. In that case the API returns the
  visible list with `following_complete: false`, `complete: false`, and the exact
  `following_count`.
- Android 0.7.3 stores the currently observed following IDs with coverage metadata.
  It shows confirmed non-reciprocal accounts against the complete followers from that
  same capture, and separately reports the number of unavailable following accounts.
  It never fills missing IDs from an older snapshot.

Response fields include `followers_complete` and `following_complete` in addition to
the existing counters, member arrays, `complete`, and `source`.

## Render service

Keep the existing `pulse-checker` service on branch `main`; do not create a replacement
service. Run backend checks with:

```bash
python -m unittest discover -s checker_backend/tests -v
```
