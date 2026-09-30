# Pulse checker backend

Pulse uses this online architecture:

Android APK -> Render pulse-checker -> online Instagram data provider -> validated snapshot.

The APK sends only the target username. It contains no Instagram session, provider token,
password, cookie, Hiker key, or Apify token.

## Provider

The backend uses Apify actors instead of HikerAPI or direct Instagram requests from Render.

Default actors:

- profile/counts: `apify~instagram-profile-scraper`
- followers and following: `thenetaji~instagram-followers-followings-scraper`
- following fallback: `publicsignallabs~instagram-following`, then
  `apify~instagram-followers-following-scraper`
- session fallback: `dami_studio~instagram-followers-following-scraper`
- final followers fallbacks: `scraping_solutions~instagram-scraper-followers-following`
  and `scraping_solutions~instagram-scraper-followers-following-no-cookies`

The backend follows `resumeCursor` for the primary relationship actor and continuation tokens
for the final followers fallbacks. It also reads all dataset pages instead of truncating at
1,000 records. Provider limits and Instagram rate limits still apply. A Free Apify account
does not imply unlimited or complete actor results; runs consume the account's credits.

## Required Render environment

- `APIFY_TOKEN`

Create a free Apify account and put its API token only in Render. Do not put the token in the APK
or GitHub.

Optional:

- `MAX_MEMBERS` (default 500000)
- `APIFY_PAGE_SIZE` (default 1000, maximum 1000)
- `APIFY_RUN_TIMEOUT` (default 150 seconds)
- `COLLECTION_TIMEOUT` (default 180 seconds, clamped to 30–180)
- `APIFY_PROFILE_ACTOR`
- `APIFY_RELATION_ACTOR`
- `APIFY_FULL_FOLLOWING_ACTOR`
- `APIFY_FULL_FOLLOWERS_ACTOR`
- `APIFY_FREE_FOLLOWING_ACTOR`
- `APIFY_OFFICIAL_RELATION_ACTOR`
- `APIFY_SESSION_ACTOR`
- `APIFY_RELATION_FALLBACK_ACTOR`
- `APIFY_SESSION_ATTEMPTS` and `APIFY_RELATION_ATTEMPTS` (default 1 each)

`IG_SESSION_JSON` is optional and used only by the authenticated following fallback. Its
`sessionid` may be at the top level or inside a `cookies` object. Keep it only in Render;
never commit it or put it in the APK. The fallback can fail when Instagram limits the session.

## Completeness model

1. Fetch profile ID plus exact followers/following counts.
2. Reject private targets.
3. Collect followers and following concurrently with at most two workers.
4. Follow available pages and merge provider results within this request only.
5. Deduplicate by stable numeric Instagram ID.
6. Require collected sizes to match the exact counts.
7. Fetch the profile again.
8. Require the same account ID and unchanged counts.
9. Return `complete: true` only after every check passes.

Partial data is never returned as a valid snapshot.

Both workers share the same request deadline, including profile rechecks, HTTP calls and
retries. The backend stops an unfinished actor run if polling fails or the deadline expires,
and always releases the collection lock. A second collection receives HTTP 429 while one
is running. Expired collections return HTTP 504; incomplete lists return HTTP 409.

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
- health check: `/health`

Do not create another Render service and do not modify the Hiker backup branch.

Run the backend checks with `python -m unittest discover -s checker_backend/tests -v`.
