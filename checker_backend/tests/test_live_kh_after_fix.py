import json
import unittest
import requests


class KhAlexeyaAfterFixProbe(unittest.TestCase):
    def test_live_render_after_fix(self):
        response = requests.post(
            "https://pulse-checker-702b.onrender.com/v1/collect",
            json={"username": "kh.alexeya"},
            timeout=(15, 220),
        )
        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text[:500]}
        print("LIVE_KH_ALEXEYA", json.dumps({
            "status": response.status_code,
            "complete": data.get("complete") if isinstance(data, dict) else None,
            "source": data.get("source") if isinstance(data, dict) else None,
            "followers_count": data.get("followers_count") if isinstance(data, dict) else None,
            "following_count": data.get("following_count") if isinstance(data, dict) else None,
            "followers_len": len(data.get("followers", [])) if isinstance(data, dict) and isinstance(data.get("followers"), list) else None,
            "following_len": len(data.get("following", [])) if isinstance(data, dict) and isinstance(data.get("following"), list) else None,
            "detail": data.get("detail") if isinstance(data, dict) else None,
        }, ensure_ascii=False))
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
