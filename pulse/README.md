# Pulse 0.7.1 — no-login Instagram collector

Pulse is a Kivy Android app that stores complete Instagram follower/following snapshots
locally and compares them by stable numeric Instagram ID.

## No Instagram login in the app

Pulse does not ask the user to sign in to Instagram. There is no Instagram WebView,
password field, session-cookie import, or saved Instagram user session in the Android app.

Collection flow:

1. the phone first tries our anonymous public Instagram collector directly from the
   phone's network;
2. if Instagram does not expose the relationship list anonymously, Pulse automatically
   calls the existing Pulse backend;
3. the backend uses our own direct collector and does not call HikerAPI, Apify, or a
   third-party scraping Actor;
4. Pulse accepts a snapshot only when followers/following list lengths exactly match the
   corresponding profile counters;
5. incomplete results such as 109/110 or 110/114 are rejected and never saved.

The user only enters the Instagram username to check and taps **Собрать данные**.

## Important limitation

Instagram can require authentication for follower/following relationship endpoints.
The Android app itself never asks the user to authenticate. The server-side fallback may
use the dedicated checker session configured in Render as `IG_SESSION_JSON`; that
session belongs to the collector infrastructure, not to the app user and is never sent to
the phone.

For public accounts the anonymous phone path is attempted first. Private accounts cannot
be enumerated anonymously.

## Local data

Snapshots and comparison history are stored in the app-private SQLite database.
A stale `instagram_session` value from Pulse 0.7.0 is deleted automatically on startup.
Android backup remains disabled.

The first complete snapshot is the baseline. Later snapshots show new followers,
unfollowers, and non-reciprocal follows. Comparison uses numeric Instagram IDs.

## Backend

The existing `checker_backend` remains on the same API contract:
`POST /v1/collect` with `{"username": "..." }`.

It is a fallback only when the anonymous phone transport cannot produce a complete
snapshot. It remains direct-only and has no HikerAPI/Apify dependency.

## Build

GitHub Actions workflow: `.github/workflows/android.yml`.

It runs unit tests, builds the arm64 debug APK with Buildozer, verifies its signature,
and uploads `pulse-android-debug` plus SHA256 checksums.

Local test command from `pulse/`:

```bash
python -m unittest discover -s tests -v
```

Current Android configuration: Kivy 2.3.1, requests 2.34.2, API 35, min API 24,
NDK 28c, arm64-v8a.
