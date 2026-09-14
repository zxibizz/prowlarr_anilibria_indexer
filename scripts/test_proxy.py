#!/usr/bin/env python3
"""Test the proxy on its own: local endpoints, the divert route, and relaying.

Uses a real HTTP proxy client, so the requests take the same shape Prowlarr's
"Http" indexer proxy produces - HTTP in absolute form, HTTPS via CONNECT.

    python3 scripts/test_proxy.py [query]

Exit code is non-zero if any check fails.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PROXY_HOST = os.environ.get("PROXY_HOST", "localhost")
PROXY_PORT = os.environ.get("PROXY_PORT", "8788")
DIVERT_PREFIX = os.environ.get("PROXY_DIVERT_PREFIX", "/_anilibria_proxy")
SITE = os.environ.get("ANILIBRIA_SITE", "https://aniliberty.top").rstrip("/")
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"
QUERY = sys.argv[1] if len(sys.argv) > 1 else "naruto"

PASS, FAIL, INFO = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m", "\033[36mINFO\033[0m"
failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"  [{PASS if ok else FAIL}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        failures.append(label)
    return ok


USER_AGENT = os.environ.get(
    "PROXY_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Relaying is what we are testing, so do not let urllib hide a redirect."""

    def redirect_request(self, *args, **kwargs):
        return None


def opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL}),
        NoRedirect,
    )


def fetch(url: str, timeout: int = 120):
    """Fetch through the proxy. Returns (status, body_bytes) or raises.

    The User-Agent matters: the real site sits behind Cloudflare, which answers
    the Python default with 403 "error code: 1010".
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener().open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# --------------------------------------------------------------------------- 1
print("\n1. local endpoints (a plain request straight at the proxy)")
with urllib.request.urlopen(f"{PROXY_URL}/healthz", timeout=15) as response:
    health = json.loads(response.read())
check(health.get("ok") is True, "/healthz answers", str(health.get("api_base")))
with urllib.request.urlopen(f"{PROXY_URL}/", timeout=15) as response:
    info = json.loads(response.read())
check(info.get("service") == "anilibria-proxy", "/ describes the proxy", info.get("service"))
check(DIVERT_PREFIX == info.get("settings", {}).get("divert_prefix"), "divert prefix agrees")


# --------------------------------------------------------------------------- 2
print(f"\n2. the divert route, in the shape Prowlarr sends it ({QUERY!r})")
route = f"http://aniliberty.top{DIVERT_PREFIX}/search?q={urllib.parse.quote(QUERY)}&limit=50"
status, body = fetch(route)
payload = json.loads(body) if status == 200 else {}
rows = payload.get("rows") or []
check(status == 200, "divert route answered by the proxy", f"HTTP {status}")
check(bool(rows), "returned rows", f"{len(rows)}")
if rows:
    print(f"         e.g. {rows[0]['torrent']['label'][:66]}")
    for key in ("release", "torrent"):
        check(key in rows[0], f"row has {key}")
    for field in ("hash", "label", "size", "seeders", "updated_at"):
        check(rows[0]["torrent"].get(field) not in (None, ""), f"torrent.{field} present")


# --------------------------------------------------------------------------- 3
print("\n3. everything else is relayed to the real site")
status, _ = fetch(f"{SITE}/")
check(status in (301, 302, 307, 308), "plain HTTP reaches the site", f"HTTP {status}")

status, body = fetch(f"{SITE}/api/v1/app/status")
relayed = status == 200 and b"is_alive" in body
check(relayed, "HTTPS via CONNECT reaches the site", f"HTTP {status}")

if rows:
    infohash = rows[0]["torrent"]["hash"]
    status, blob = fetch(f"{SITE}/api/v1/anime/torrents/{infohash}/file")
    is_torrent = status == 200 and blob[:1] == b"d" and b"announce" in blob[:4096]
    check(is_torrent, "torrent downloads through the proxy", f"{len(blob)} bytes")


# --------------------------------------------------------------------------- summary
print("\n" + "=" * 66)
if failures:
    print(f"RESULT: {len(failures)} check(s) failed")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("RESULT: all checks passed")
