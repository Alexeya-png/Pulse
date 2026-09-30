# Pulse 0.7.3 — no-login Instagram tracker

Pulse never asks the app user to sign in to Instagram. There is no Instagram WebView,
password field, cookie import, or saved user Instagram session.

## Collection flow

1. The phone first tries an anonymous direct request.
2. If Instagram requires authentication, Pulse calls our existing Render collector.
3. The backend uses only our direct Instagram collector. HikerAPI, Apify, and third-party
   scraper Actors are not part of the normal path.
4. The followers list is strict: it is saved only when its length exactly matches the
   Instagram profile counter. This prevents false follow/unfollow events.
5. Instagram can hide some following accounts from the technical checker identity.
   The app stores only the currently observed IDs, with completeness and the exact
   expected count. It never copies hidden IDs from an older snapshot.
6. Confirmed non-reciprocal accounts are the observed following IDs minus the complete
   followers list from the same capture. This works on the first capture and when only
   part of the following list is available. The UI marks the count with `+` and reports
   how many following accounts remain unknown. An empty partial result never means
   that everyone follows back.
7. Unfollowers and new followers compare two complete followers snapshots by numeric
   ID. The first capture is a baseline; incomplete followers never overwrite it or
   create events. No changes on a later capture means no new events; history is kept.

The app user only enters an Instagram username and taps **Собрать данные**.

## Local data

Snapshots and comparison history remain in the app-private SQLite database. A stale
`instagram_session` value from Pulse 0.7.0 is deleted automatically. Android backup is
disabled.

SQLite schema 2 preserves existing followers and event history. Following lists that
0.7.2 marked `local-reconciled` are excluded from current reciprocal results until a
fresh collection replaces them. Older app versions cannot open the new schema.

Comparison uses stable numeric Instagram IDs. Username changes alone are not treated as
unfollows.

## Build

GitHub Actions workflow: `.github/workflows/android.yml`.

It runs unit tests, builds the arm64 debug APK with Buildozer, verifies its signature,
and uploads `pulse-android-debug` plus SHA256 checksums.
