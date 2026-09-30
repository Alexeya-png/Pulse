import json
import sys
import unittest
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pulse"))

from pulse.instagram_direct import InstagramDirectError, collect_snapshot_direct


class KhAlexeyaLiveProbe(unittest.TestCase):
    def test_kh_alexeya_no_login_probe(self):
        target = "kh.alexeya"

        print("\n=== KH_ALEXEYA_ANONYMOUS_DEVICE_STYLE ===")
        try:
            snapshot = collect_snapshot_direct(target, timeout_seconds=90)
            followers = next(s for s in snapshot.samples if s.kind == "followers")
            following = next(s for s in snapshot.samples if s.kind == "following")
            print(json.dumps({
                "ok": True,
                "source": snapshot.source,
                "followers": len(followers.members),
                "following": len(following.members),
            }, ensure_ascii=False))
        except InstagramDirectError as exc:
            print(json.dumps({
                "ok": False,
                "error": str(exc),
            }, ensure_ascii=False))

        print("=== KH_ALEXEYA_RENDER_FALLBACK ===")
        try:
            response = requests.post(
                "https://pulse-checker-702b.onrender.com/v1/collect",
                json={"username": target},
                timeout=(15, 220),
            )
            try:
                data = response.json()
            except ValueError:
                data = {"raw": response.text[:500]}
            summary = {
                "status": response.status_code,
                "complete": data.get("complete") if isinstance(data, dict) else None,
                "source": data.get("source") if isinstance(data, dict) else None,
                "followers_count": data.get("followers_count") if isinstance(data, dict) else None,
                "following_count": data.get("following_count") if isinstance(data, dict) else None,
                "followers_len": len(data.get("followers", [])) if isinstance(data, dict) and isinstance(data.get("followers"), list) else None,
                "following_len": len(data.get("following", [])) if isinstance(data, dict) and isinstance(data.get("following"), list) else None,
                "detail": data.get("detail") if isinstance(data, dict) else None,
            }
            print(json.dumps(summary, ensure_ascii=False))
        except requests.RequestException as exc:
            print(json.dumps({
                "status": None,
                "error": f"{exc.__class__.__name__}: {exc}",
            }, ensure_ascii=False))

        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
