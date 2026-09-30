import json
import getpass
import instaloader

username = input("Instagram username: ").strip().lstrip("@").lower()
password = getpass.getpass("Instagram password: ")

loader = instaloader.Instaloader()
try:
    loader.login(username, password)
except instaloader.exceptions.TwoFactorAuthRequiredException:
    code = input("2FA code: ").strip()
    loader.two_factor_login(code)

logged_in = loader.test_login()
if not logged_in:
    raise SystemExit("Login failed.")

print("\nCopy the line below into Render as IG_SESSION_JSON:\n")
print(json.dumps(loader.save_session(), separators=(",", ":")))
