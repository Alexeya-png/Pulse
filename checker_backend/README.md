# Pulse checker backend

Pulse keeps the same online architecture as the previously working HikerAPI version:

Android APK -> Render pulse-checker -> online Instagram data provider -> validated snapshot.

The APK sends only the target username. It contains no Instagram session, provider token,
password, cookie, Hiker key, or Apify token.

## Provider

The backend uses Apify actors instead of HikerAPI or direct Instagram requests from Render.

Default actors:

- profile/counts: `apify~instagram-profile-scraper`
- followers/following: `scraping_solutions~instagram-scraper-followers-following-no-cookies`

The relationship actor works with public profiles and supports continuation tokens. The backend
follows continuations until the whole list has been collected.

## Required Render environment

- `APIFY_TOKEN`

Create a free Apify account and put its API token only in Render. Do not put the token in the APK
or GitHub.

Optional:

- `MAX_MEMBERS` (default 500000)
- `APIFY_PAGE_SIZE` (default 1000, maximum 1000)
- `APIFY_RUN_TIMEOUT` (default 150 seconds)
- `APIFY_PROFILE_ACTOR`
- `APIFY_RELATION_ACTOR`

The old `IG_SESSION_JSON` is not used by this collector.

## Completeness model

The backend deliberately mirrors the old Hiker flow:

1. Fetch profile ID plus exact followers/following counts.
2. Reject private targets.
3. Fetch every followers page until continuation ends.
4. Fetch every following page until continuation ends.
5. Deduplicate by stable numeric Instagram ID.
6. Require collected sizes to match the exact counts.
7. Fetch the profile again.
8. Require the same account ID and unchanged counts.
9. Return `complete: true` only after every check passes.

Partial data is never returned as a valid snapshot.

## API compatibility

`POST /v1/collect` remains compatible with the Android app:

- account
- captured_at
- followers_count
- following_count
- followers
- following
- complete
- source

Each member remains `{id, username}`.

`GET /health` reports whether the online provider token is configured.

## Render

Keep the existing service:

- service: `pulse-checker`
- branch: `main`
- build: `python -m checker_backend.build`
- start: `uvicorn checker_backend.main:app --host 0.0.0.0 --port $PORT --workers 1`

Do not create another Render service and do not modify the Hiker backup branch.
