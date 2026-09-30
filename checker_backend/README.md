# Pulse checker backend

The backend uses one dedicated Instagram checker account to read followers and
following lists that account is allowed to see. Pulse stores snapshots locally
on Android and compares only complete snapshots.

## Environment

Required:

- IG_SESSION_JSON: JSON containing the checker account sessionid and any other
  Instagram cookies.

Optional:

- MAX_MEMBERS (default: 500000)

Never commit the session to GitHub. Keep IG_SESSION_JSON only in Render.

## Collector

The collector is self-hosted and has no HikerAPI dependency or fallback.

It uses direct authenticated HTTP requests to Instagram with the checker session.
It does not use Chromium, Playwright, Firefox, WebKit, DOM scraping, or browser
downloads.

Profile resolution tries Instagram web_profile_info on the web and i.instagram
hosts. If that identifies the account but exact counts are missing, the collector
uses the Instagram user info endpoint. Search is only a last-resort way to resolve
the numeric user ID; exact follower/following counts are still required before a
snapshot starts.

Followers and following are fetched from Instagram friendship endpoints with
next_max_id pagination. Members are keyed by stable Instagram numeric IDs, not
usernames.

The completeness rules match the previously working collector:

1. Read exact follower and following counts before collection.
2. Fetch every page until Instagram returns no next_max_id.
3. Reject repeated cursors, malformed pages, rate limits, and partial lists.
4. Read the exact profile counts again after collection.
5. Reject the snapshot if the account ID or either count changed during collection.
6. Return success only when both collected list sizes equal the exact counts.

Private targets are not rejected in advance. If the checker account is allowed to
read their lists, collection can proceed; otherwise Instagram's access response is
returned as an error.

## Run

~~~bash
python -m checker_backend.build
uvicorn checker_backend.main:app --host 0.0.0.0 --port 10000 --workers 1
~~~

The build installs only Python dependencies. There is no browser installation.

## Render

Keep the existing pulse-checker service and URL.

- Build command: python -m checker_backend.build
- Start command: uvicorn checker_backend.main:app --host 0.0.0.0 --port $PORT --workers 1
- Branch: main
- Region: Frankfurt
- IG_SESSION_JSON remains configured only as a Render secret

Do not create a replacement service and do not use the Hiker backup branch.

## Compatibility

GET /health remains:

- ok
- engine = pulse-web-collector
- session_configured
- hiker_dependency = false

POST /v1/collect keeps the Android response shape:

- account
- captured_at
- followers_count
- following_count
- followers
- following
- complete
- source

Member objects remain id + username. IDs now use Instagram's stable numeric user ID,
matching the behavior of the previously working data collector more closely.

Backend-only commits do not rebuild the APK.
