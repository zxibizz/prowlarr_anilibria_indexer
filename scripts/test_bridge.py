#!/usr/bin/env python3
"""Smoke-test the anilibria-bridge service from the host."""
import json
import sys
import urllib.parse
import urllib.request

BASE = "http://localhost:8787"


def get(path, **params):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


status, health = get("/healthz")
print(f"/healthz            HTTP {status}  {health}")

status, latest = get("/search", limit=5)
print(f"/search (latest)    HTTP {status}  rows={latest['meta']['count']}")
if latest["rows"]:
    row = latest["rows"][0]
    print("  release:", json.dumps(row["release"], ensure_ascii=False)[:200])
    print("  torrent:", json.dumps(row["torrent"], ensure_ascii=False)[:200])

for term in ("naruto", "kizumonogatari"):
    status, data = get("/search", q=term, limit=100)
    meta = data["meta"]
    print(f"/search q={term:<16} HTTP {status}  rows={meta['count']} cached={meta['cached']}")
    if not data["rows"]:
        print("  !! no rows")
        continue
    for row in data["rows"][:4]:
        torrent = row["torrent"]
        release = row["release"]
        print(
            f"  - {torrent['label'][:72]:<72} "
            f"size={torrent['size']} s={torrent['seeders']} "
            f"type={release['type']} hash={str(torrent['hash'])[:12]}"
        )
    # every row must be usable by the YAML definition
    required = {
        "torrent.hash": data["rows"][0]["torrent"]["hash"],
        "torrent.label": data["rows"][0]["torrent"]["label"],
        "torrent.size": data["rows"][0]["torrent"]["size"],
        "torrent.seeders": data["rows"][0]["torrent"]["seeders"],
        "release.alias": data["rows"][0]["release"]["alias"],
        "release.type": data["rows"][0]["release"]["type"],
        "torrent.updated_at": data["rows"][0]["torrent"]["updated_at"],
    }
    missing = [k for k, v in required.items() if v in (None, "")]
    if missing:
        print(f"  !! missing fields on row 0: {missing}")
        sys.exit(1)

print("\nbridge OK")
