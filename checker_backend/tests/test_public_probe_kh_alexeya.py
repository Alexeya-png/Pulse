import json
import re
import sys
import unittest
from urllib.parse import quote

import requests


UA_BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
UA_MOBILE = "Instagram 389.0.0.49.87 Android (34/14; 420dpi; 1080x2400; Google/google; Pixel 7; panther; panther; en_US; 699134704)"
APP_ID = "936619743392459"
TARGET = "kh.alexeya"


def safe_json(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def extract_user(data):
    if not isinstance(data, dict):
        return None
    candidates = []
    if isinstance(data.get("data"), dict) and isinstance(data["data"].get("user"), dict):
        candidates.append(data["data"]["user"])
    if isinstance(data.get("user"), dict):
        candidates.append(data["user"])
    for item in data.get("users") or []:
        if isinstance(item, dict):
            user = item.get("user") if isinstance(item.get("user"), dict) else item
            candidates.append(user)
    for user in candidates:
        if str(user.get("username") or "").lower() == TARGET:
            return user
    return None


class KhAlexeyaPublicEndpointProbe(unittest.TestCase):
    def test_public_endpoints(self):
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA_BROWSER,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
            "X-IG-App-ID": APP_ID,
            "X-Requested-With": "XMLHttpRequest",
        })
        user_id = None

        endpoints = [
            ("web_profile_www", "https://www.instagram.com/api/v1/users/web_profile_info/", {"username": TARGET}),
            ("web_profile_i", "https://i.instagram.com/api/v1/users/web_profile_info/", {"username": TARGET}),
            ("topsearch", "https://www.instagram.com/api/v1/web/search/topsearch/", {"context": "blended", "query": TARGET, "include_reel": "false"}),
        ]
        for name, url, params in endpoints:
            try:
                r = s.get(url, params=params, timeout=25, allow_redirects=True)
                data = safe_json(r)
                user = extract_user(data)
                if user and not user_id:
                    user_id = str(user.get("pk") or user.get("id") or user.get("pk_id") or "") or None
                print("PROBE", json.dumps({
                    "name": name,
                    "status": r.status_code,
                    "final": r.url.split("?")[0],
                    "user_id": str(user.get("pk") or user.get("id") or user.get("pk_id") or "") if user else None,
                    "username": user.get("username") if user else None,
                    "followers": user.get("follower_count") if user else None,
                    "following": user.get("following_count") if user else None,
                    "keys": sorted(list(data.keys()))[:20] if isinstance(data, dict) else None,
                }, ensure_ascii=False))
            except Exception as exc:
                print("PROBE", json.dumps({"name": name, "error": type(exc).__name__}))

        try:
            r = s.get(f"https://www.instagram.com/{TARGET}/", timeout=25, allow_redirects=True)
            text = r.text
            patterns = [
                r'"profile_id":"(\d+)"',
                r'"id":"(\d+)","username":"kh\.alexeya"',
                r'"pk":"?(\d+)"?[^\n]{0,500}"username":"kh\.alexeya"',
            ]
            ids = []
            for p in patterns:
                ids.extend(re.findall(p, text, re.I))
            if ids and not user_id:
                user_id = ids[0]
            print("PROBE", json.dumps({
                "name": "profile_html",
                "status": r.status_code,
                "length": len(text),
                "ids": ids[:5],
                "login_redirect": "/accounts/login" in r.url,
                "has_following_word": "Following" in text,
                "has_username": TARGET in text,
            }, ensure_ascii=False))
        except Exception as exc:
            print("PROBE", json.dumps({"name": "profile_html", "error": type(exc).__name__}))

        if not user_id:
            print("PROBE", json.dumps({"name": "relations", "skipped": "no_user_id"}))
            self.assertTrue(True)
            return

        relation_variants = []
        for host in ("https://i.instagram.com/api/v1/friendships", "https://www.instagram.com/api/v1/friendships"):
            relation_variants.append((f"friendships_{host.split('//')[1].split('.')[0]}", f"{host}/{user_id}/following/", {"count": 200, "search_surface": "follow_list_page"}))

        for name, url, params in relation_variants:
            for ua_name, ua in (("browser", UA_BROWSER), ("mobile", UA_MOBILE)):
                h = dict(s.headers)
                h["User-Agent"] = ua
                try:
                    r = requests.get(url, params=params, headers=h, timeout=25, allow_redirects=True)
                    data = safe_json(r)
                    users = data.get("users") if isinstance(data, dict) else None
                    print("PROBE", json.dumps({
                        "name": name + "_" + ua_name,
                        "status": r.status_code,
                        "rows": len(users) if isinstance(users, list) else None,
                        "next_max_id": data.get("next_max_id") if isinstance(data, dict) else None,
                        "message": data.get("message") if isinstance(data, dict) else None,
                        "final": r.url.split("?")[0],
                    }, ensure_ascii=False))
                except Exception as exc:
                    print("PROBE", json.dumps({"name": name + "_" + ua_name, "error": type(exc).__name__}))

        hashes = {
            "gql_following_a": "d04b0a864b4b54837c0d870b0e77e076",
            "gql_following_b": "58712303d941c6855d4e888c5f0cd22f",
            "gql_followers_a": "c76146de99bb02f6415203be841dd25a",
            "gql_followers_b": "37479f2b8209594dde7facb0d904896a",
        }
        for name, qh in hashes.items():
            variables = {"id": user_id, "include_reel": True, "fetch_mutual": False, "first": 200}
            url = "https://www.instagram.com/graphql/query/"
            try:
                r = s.get(url, params={"query_hash": qh, "variables": json.dumps(variables,separators=(",",":"))}, timeout=25)
                data = safe_json(r)
                user = data.get("data", {}).get("user") if isinstance(data, dict) else None
                edges = None
                if isinstance(user, dict):
                    for key in ("edge_follow", "edge_followed_by"):
                        edge = user.get(key)
                        if isinstance(edge, dict) and isinstance(edge.get("edges"), list):
                            edges = edge["edges"]
                            break
                print("PROBE", json.dumps({
                    "name": name,
                    "status": r.status_code,
                    "rows": len(edges) if isinstance(edges, list) else None,
                    "keys": sorted(list(user.keys()))[:20] if isinstance(user, dict) else None,
                    "message": data.get("message") if isinstance(data, dict) else None,
                }, ensure_ascii=False))
            except Exception as exc:
                print("PROBE", json.dumps({"name": name, "error": type(exc).__name__}))

        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
