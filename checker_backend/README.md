# Pulse checker backend

The backend is the no-login fallback for Pulse:

Android APK -> Pulse backend -> direct Instagram collector.

It does not use HikerAPI, Apify, public Actors, or a paid scraping provider.

## Render environment

- `IG_SESSION_JSON`: technical checker-session cookies kept only on Render.
- `COLLECTION_TIMEOUT`: optional timeout.
- `IG_HTTP_TRANSPORT`: `requests` (default) or `curl_cffi` for a controlled
  browser-transport compatibility check. The latter uses pinned Chrome 136
  headers, TLS and HTTP/2 settings; it does not run JavaScript or repair a session.

The checker identity is infrastructure owned by the collector; it is not the Android
user's Instagram session and is never sent to the phone.

## Profile lookup

Backend 0.7.6 first reads the ordinary public profile page without checker cookies.
It extracts the displayed profile's numeric ID from page route data and exact
followers/following counts from the page metadata. When these are available, the
collector does not call profile API endpoints, including during the final recheck.
The page URL and canonical metadata must match the requested username; ambiguous
IDs, login redirects and abbreviated counts such as `1.2K` are rejected.

A rate limit on the public page stops the check. When its metadata is unavailable,
the existing checker-session page fallback also attempts to extract the identity
before using profile APIs. Page lookup does not guarantee that
Instagram will provide the relationship lists: those requests can still fail or
return partial results. The existing completeness rules remain in force.

## Access failures and backoff

An Instagram HTTP 429 stops the active check immediately. The server respects
`Retry-After` (seconds or HTTP date); when it is absent or invalid, the default
pause is 15 minutes. During this pause, checks for every username return 503 with
a readable `detail` and `Retry-After`, without sending another Instagram request.
The old two-second retry and transport switching after 429 have been removed.

Authenticated HTTP 401 and confirmation/consent responses stop collection and
explain that the technical account needs attention. Authentication is recorded
before the response, since Instagram can clear the session cookie on rejection.
When every checker profile-page response
redirects to login, the error asks for the checker session to be checked. Those
errors use a five-minute pause. Authenticated HTTP 403 and `feedback_required`
stop after the first response with a fifteen-minute pause. JSON `login_required`
and authenticated login redirects also stop immediately. This detects server access failures; it does not
prove that the session has expired, or distinguish every IP restriction.

Diagnostics record only response status and fixed page categories, never full
redirect URLs, query parameters, cookies or response bodies. The pause is shared
in memory by the existing single-worker service; a process restart clears it.
Successful response fields and snapshot completeness requirements are unchanged.

## Browser transport experiment

`curl_cffi==0.16.3` with preset `chrome136` is selected before collection for both
anonymous profile lookup and authenticated requests. Its own browser headers are
preserved instead of overriding them with the legacy Chrome 140 or Android UA.
In this mode, cookie/header identity variants are disabled and network retry is
zero. Access failures never cause a switch to `requests` or another identity.
`/health` reports `version` and `http_transport`; it does not test session validity.

Enable `IG_HTTP_TRANSPORT=curl_cffi` on the existing Render service, deploy, and
perform one ordinary `/v1/collect` check. Compare only response categories and
counts in logs. If authenticated requests still return 401, the transport change
has not resolved access: the technical-account owner must check the same account
in the official Instagram browser session. A browser success and server failure
would help isolate environment or endpoint differences; neither proves the exact
reason for an Instagram restriction. Never put session cookies in a public
diagnostic route, repository, logs or phone payload. Set the variable back to
`requests` to revert the transport without changing APK data or response fields.

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
