# Pulse 0.7.0 — direct Instagram collector

Pulse is a Kivy Android app that stores complete Instagram follower/following snapshots
locally and compares them by stable numeric Instagram ID.

## Architecture

Normal collection no longer goes through Render, HikerAPI, Apify, or any scraper Actor:

Android phone -> Instagram -> Pulse local SQLite.

On the first collection Pulse opens an embedded Instagram WebView. Sign in to the same
Instagram account whose username is entered in Pulse and press **Готово**. Pulse keeps
only the required session cookies and the WebView user agent in the app's private storage.
Android backup is disabled for the app.

The collector then:

1. verifies the signed-in Instagram account against the target username and numeric ID;
2. reads exact followers/following counters;
3. paginates both relationship lists directly from Instagram;
4. merges supported Instagram relationship hosts by numeric user ID;
5. requires exact list lengths;
6. rechecks the profile counters after the lists are loaded;
7. creates a Snapshot only when every completeness check passes.

If Instagram returns 109/110, 110/114, a rate limit, an expired session, or changing
counts, Pulse does **not** save that result and therefore does not create false
follow/unfollow events.

## First use

1. Install the 0.7.0 APK.
2. Enter your own Instagram username.
3. Tap **Собрать данные**.
4. In the embedded Instagram window, sign in to that same account.
5. Tap **Готово**.
6. Pulse automatically starts the first full collection.

Later collections reuse the saved session. Use **Настройки -> Перевойти в Instagram**
when Instagram expires the session, or **Забыть вход Instagram** to remove it from Pulse.

## Local data

Snapshots and comparison history are stored in the app-private SQLite database. The
Instagram session is stored in the app-private preferences file and is not sent to the
Pulse backend during normal collection. `android.allow_backup = False` and
`android.private_storage = True` are enabled in `buildozer.spec`.

The first complete snapshot is the baseline. Following snapshots show new followers,
unfollowers, and non-reciprocal follows. Comparison is by numeric Instagram ID, so a
username change alone is not treated as an unfollow.

## Backend

`checker_backend` remains API-compatible as a direct-only diagnostic/fallback service.
It no longer uses Apify/Hiker/public Actors. Because a cloud IP or checker session can see
a truncated Instagram relationship list, the Android app does not depend on that service
for normal collection.

## Build

GitHub Actions workflow: `.github/workflows/android.yml`.

It runs unit tests, builds the arm64 debug APK with Buildozer, verifies its signature, and
uploads `pulse-android-debug` plus SHA256 checksums.

Local test command from `pulse/`:

```bash
python -m unittest discover -s tests -v
```

Current Android configuration: Kivy 2.3.1, requests 2.34.2, API 35, min API 24,
NDK 28c, arm64-v8a.
