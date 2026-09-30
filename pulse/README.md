# Pulse 0.7.2 — no-login Instagram tracker

Pulse never asks the app user to sign in to Instagram. There is no Instagram WebView,
password field, cookie import, or saved user Instagram session.

## Collection flow

1. The phone first tries an anonymous direct request.
2. If Instagram requires authentication, Pulse calls our existing Render collector.
3. The backend uses only our direct Instagram collector. HikerAPI, Apify, and third-party
   scraper Actors are not part of the normal path.
4. The followers list is strict: it is saved only when its length exactly matches the
   Instagram profile counter. This prevents false follow/unfollow events.
5. Instagram can hide some following accounts from the technical checker identity. For
   example, kh.alexeya currently reports 114 following while that checker can see 110.
   This no longer blocks follower tracking.
6. If the phone already has a previous complete following snapshot of the same expected
   size, Pulse can carry forward only the IDs that are hidden now, but only when every
   currently visible ID already existed in that full baseline. If a new visible ID makes
   the result ambiguous, Pulse does not guess.
7. When safe reconciliation is impossible, Pulse saves the complete followers sample and
   temporarily disables the non-reciprocal calculation instead of inventing missing users.

The app user only enters an Instagram username and taps **Собрать данные**.

## Local data

Snapshots and comparison history remain in the app-private SQLite database. A stale
`instagram_session` value from Pulse 0.7.0 is deleted automatically. Android backup is
disabled.

Comparison uses stable numeric Instagram IDs. Username changes alone are not treated as
unfollows.

## Build

GitHub Actions workflow: `.github/workflows/android.yml`.

It runs unit tests, builds the arm64 debug APK with Buildozer, verifies its signature,
and uploads `pulse-android-debug` plus SHA256 checksums.
