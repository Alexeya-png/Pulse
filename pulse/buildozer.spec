[app]
title = Pulse
package.name = pulse
package.domain = app.localtracker
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json
source.exclude_dirs = tests,tools,.git,.github,.venv,__pycache__,docs,bin
version = 0.2.0
requirements = python3,kivy==2.3.1,sqlite3,openssl,pyjnius,requests==2.34.2,certifi,charset-normalizer,idna,urllib3
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 35
android.minapi = 24
android.ndk = 28c
android.ndk_api = 24
android.archs = arm64-v8a
android.allow_backup = False
android.private_storage = True
android.accept_sdk_license = True
p4a.branch = develop

[buildozer]
log_level = 2
warn_on_root = 1
