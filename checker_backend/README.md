# Pulse checker backend

The backend is the no-login fallback for Pulse 0.7.2:

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
- The Android app may reconcile hidden following IDs only from its own previous complete
  local following snapshot under strict subset/count checks. Otherwise it stores only the
  complete followers sample and disables non-reciprocal results for that capture.

Response fields include `followers_complete` and `following_complete` in addition to
the existing counters, member arrays, `complete`, and `source`.

## Render service

Keep the existing `pulse-checker` service on branch `main`; do not create a replacement
service. Run backend checks with:

```bash
python -m unittest discover -s checker_backend/tests -v
```
