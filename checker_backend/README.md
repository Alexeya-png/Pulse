# Pulse checker backend

The backend uses one dedicated Instagram account to read the followers and
following lists that account is allowed to see. Pulse keeps snapshots locally
on the Android device and compares only complete snapshots.

## Environment

Required:

- `IG_CHECKER_USERNAME`
- either `IG_SESSION_JSON` (preferred) or `IG_CHECKER_PASSWORD`

Optional:

- `MAX_MEMBERS` (default: 500000)

Do not commit the checker account password or session cookies to GitHub. Put
them directly into Render environment variables.

## Run

```bash
pip install -r checker_backend/requirements.txt
uvicorn checker_backend.main:app --host 0.0.0.0 --port 10000
```

For private target accounts, the checker account must already follow the
target. The service never treats a partial fetch as an unfollow event: if the
returned list length does not match Instagram's count, the request fails and
Pulse keeps the previous snapshot.
