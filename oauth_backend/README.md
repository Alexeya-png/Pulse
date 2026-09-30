# Pulse OAuth backend

This backend keeps the Meta App Secret out of the Android APK.

## Meta setup

Use **Instagram API with Instagram Login / Business Login for Instagram**.

Set the valid OAuth redirect URI in Meta to:

```
https://YOUR-BACKEND.example/instagram/callback
```

The Android app returns through the custom URI:

```
pulse://oauth
```

## Environment variables

```
IG_APP_ID=...
IG_APP_SECRET=...
IG_REDIRECT_URI=https://YOUR-BACKEND.example/instagram/callback
APP_RETURN_URI=pulse://oauth
```

Run locally:

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

The Meta App Secret must stay on this server. Never put it in `oauth_config.py`,
the APK, GitHub client-side files, or screenshots.
