"""Android session storage encrypted with a non-exportable Android Keystore key."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .model import DataError


class SessionVault:
    def __init__(self, path: Path, android: bool):
        self.path = path
        self.android = android
        self.memory = None

    def _key(self):
        from jnius import autoclass

        key_store = autoclass("java.security.KeyStore").getInstance("AndroidKeyStore")
        key_store.load(None)
        alias = "pulse.session.v1"
        if not key_store.containsAlias(alias):
            props = autoclass("android.security.keystore.KeyProperties")
            builder = autoclass("android.security.keystore.KeyGenParameterSpec$Builder")(alias, props.PURPOSE_ENCRYPT | props.PURPOSE_DECRYPT)
            builder.setBlockModes(["GCM"])
            builder.setEncryptionPaddings(["NoPadding"])
            builder.setKeySize(256)
            generator = autoclass("javax.crypto.KeyGenerator").getInstance("AES", "AndroidKeyStore")
            generator.init(builder.build())
            generator.generateKey()
        return key_store.getKey(alias, None)

    def save(self, settings: dict):
        if not self.android:
            self.memory = settings
            return
        from jnius import autoclass

        cipher_cls = autoclass("javax.crypto.Cipher")
        base64 = autoclass("android.util.Base64")
        cipher = cipher_cls.getInstance("AES/GCM/NoPadding")
        cipher.init(cipher_cls.ENCRYPT_MODE, self._key())
        raw = json.dumps(settings).encode("utf-8")
        encrypted = cipher.doFinal(raw)
        payload = {"iv": str(base64.encodeToString(cipher.getIV(), 2)), "data": str(base64.encodeToString(encrypted, 2))}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp, self.path)

    def load(self) -> dict | None:
        if not self.android:
            return self.memory
        if not self.path.exists():
            return None
        try:
            from jnius import autoclass

            payload = json.loads(self.path.read_text(encoding="utf-8"))
            base64 = autoclass("android.util.Base64")
            cipher_cls = autoclass("javax.crypto.Cipher")
            cipher = cipher_cls.getInstance("AES/GCM/NoPadding")
            spec = autoclass("javax.crypto.spec.GCMParameterSpec")(128, base64.decode(payload["iv"], 2))
            cipher.init(cipher_cls.DECRYPT_MODE, self._key(), spec)
            raw = cipher.doFinal(base64.decode(payload["data"], 2))
            return json.loads(bytes(int(b) & 255 for b in raw).decode("utf-8"))
        except Exception:
            raise DataError("Не удалось открыть сохранённую сессию. Подключите аккаунт снова.") from None

    def clear(self):
        self.memory = None
        self.path.unlink(missing_ok=True)
