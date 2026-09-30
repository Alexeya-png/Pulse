# Pulse checker backend

The backend uses one dedicated Instagram account to read the followers and
following lists that account is allowed to see. Pulse keeps snapshots locally
on the Android device and compares only complete snapshots.

## Environment

Required:

- `IG_SESSION_JSON`: a JSON object containing the checker account's `sessionid`
  and any other session cookies. Password login is not implemented.

Optional:

- `MAX_MEMBERS` (default: 500000)

Do not commit session cookies to GitHub. Put them directly into Render environment
variables. This service has no HikerAPI calls, keys, or fallback. It reads the
Instagram web pages with Playwright using the checker's own session.

## Run

```bash
python -m checker_backend.build
uvicorn checker_backend.main:app --host 0.0.0.0 --port 10000 --workers 1
```

The build installs pinned Python dependencies and runs
`python -m playwright install --only-shell chromium`. Only Chromium headless
shell and Playwright's small required helpers are installed, not full Chromium,
Firefox, or WebKit. Linux machines missing OS libraries can use
`python -m checker_backend.build --with-deps` (requires root/sudo).

Browser files live in `checker_backend/.browsers`, included in the deployed build
but ignored by Git. Both build and runtime select this directory explicitly;
an old `PLAYWRIGHT_BROWSERS_PATH=/tmp/...` environment value is ignored.
No browser downloads happen during startup or collection.

The build and application startup both launch the shell and render a local test
page. A missing executable or system library prevents deployment readiness instead
of failing the first user request. Each collection owns and closes its browser,
including on errors; sync Playwright objects are never shared between request
threads. One collection runs at a time (concurrent requests still receive 429).
Keep Uvicorn at one worker to preserve this limit. Browser memory is released
between collections; this does not guarantee lower peak memory during collection.

## Render

`render.yaml` records the configuration for the existing `pulse-checker` service:

- Build command: `python -m checker_backend.build`
- Start command: `uvicorn checker_backend.main:app --host 0.0.0.0 --port $PORT --workers 1`
- Health check: `/health`
- Branch: `main`; Python runtime; Frankfurt; existing Free plan.

For a manually created service, apply these settings in Render once; adding a
Blueprint file alone does not update that service. Keep the existing service/URL
and its `IG_SESSION_JSON`. Do not create a replacement service or use the Hiker
backup branch. A successful build logs the installed components and size;
startup logs `Chromium headless shell smoke check passed` before readiness.

## Compatibility and tests

`GET /health` and `POST /v1/collect` with `{"username":"example"}` keep their
existing contract, including snapshot fields, member IDs/usernames, completeness
checks, and HTTP errors with a `detail` field. APK code and its backend URL are
unchanged. Backend-only commits do not trigger an APK rebuild.

```bash
python -m pip install httpx==0.28.1
python -m unittest discover -s checker_backend/tests -v
```

Tests cover HTTP compatibility, partial-snapshot rejection, concurrent requests,
cleanup, startup failure, real headless rendering and DOM extraction, and browser
use across distinct request threads. They never connect to Instagram.

For private target accounts, the checker account must already follow the
target. The service never treats a partial fetch as an unfollow event: if the
returned list length does not match Instagram's count, the request fails and
Pulse keeps the previous snapshot.
