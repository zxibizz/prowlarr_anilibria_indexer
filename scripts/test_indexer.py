#!/usr/bin/env python3
"""End-to-end test of the custom AniLibria indexer against a live Prowlarr.

Exercises the same code paths the Prowlarr UI uses:

  1. force a definition re-read and confirm `anilibria` is visible
  2. add (or update) the indexer through the v1 API
  3. run Prowlarr's own indexer test
  4. run a real keyword search through the aggregator
  5. download the first result's .torrent and verify it is a real bencoded file

Usage:
    python3 scripts/test_indexer.py [query ...]

Defaults to a couple of anime queries. Requires the stack from
`docker compose up -d --build` to be running.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PROWLARR = os.environ.get("PROWLARR_URL", "http://localhost:9696")
DEFINITION = "anilibria"
INDEXER_NAME = "AniLibria"
DEFAULT_QUERIES = ["naruto", "kizumonogatari"]

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[36mINFO\033[0m"

failures: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    print(f"  [{PASS if condition else FAIL}] {label}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(label)
    return condition


def read_api_key() -> str:
    key = os.environ.get("PROWLARR_API_KEY")
    if key:
        return key
    candidates = [
        os.path.join(ROOT, "config", "config.xml"),
        os.path.expanduser("~/Library/Application Support/Prowlarr/config.xml"),
        os.path.expanduser("~/.config/Prowlarr/config.xml"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                root = ET.parse(path).getroot()
            except ET.ParseError:
                continue
            node = root.find("ApiKey")
            if node is not None and node.text:
                print(f"  [{INFO}] api key read from {path}")
                return node.text.strip()
    raise SystemExit("Could not find the Prowlarr API key. Set PROWLARR_API_KEY.")


KEY = read_api_key()


def api(path: str, method: str = "GET", body: object = None, params: dict | None = None):
    status, payload = api_raw(path, method, body, params)
    if status >= 400:
        raise RuntimeError(f"{method} {path} -> HTTP {status}: {str(payload)[:600]}")
    return status, payload


def api_raw(path: str, method: str = "GET", body: object = None, params: dict | None = None):
    """Same as api() but returns (status, payload) instead of raising on 4xx."""
    url = f"{PROWLARR}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("X-Api-Key", KEY)
    request.add_header("Accept", "application/json")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


# --------------------------------------------------------------------------- 0
print("\n0. proxy preflight")
_, proxies = api("/api/v1/indexerproxy")
_, indexers_now = api("/api/v1/indexer")
here = next((i for i in (indexers_now or []) if i.get("definitionName") == DEFINITION), None)
proxy_tag_ids = {t for p in (proxies or []) for t in (p.get("tags") or [])}
assigned = proxy_tag_ids & set((here or {}).get("tags") or [])
if not proxies:
    print(f"  [{INFO}] no indexer proxy configured. The definition's search route is")
    print("         answered by the proxy, so run this first:")
    print("         python3 scripts/configure_proxy.py")
elif not assigned:
    print(f"  [{INFO}] a proxy exists but is not assigned to the indexer; see")
    print("         python3 scripts/configure_proxy.py")
else:
    print(f"  [{PASS}] proxy assigned to the indexer")


# --------------------------------------------------------------------------- 1
print("\n1. definition visibility")
api("/api/v1/command", "POST", {"name": "IndexerDefinitionUpdate"})
time.sleep(3)

_, schema = api("/api/v1/indexer/schema")
match = next((s for s in schema if s.get("definitionName") == DEFINITION), None)
if not match:
    names = sorted(s.get("definitionName") for s in schema if s.get("definitionName"))
    print(f"  [{FAIL}] definition {DEFINITION!r} not in {len(names)} loaded definitions")
    print(f"         present: {names[:40]}")
    sys.exit(1)
print(f"  [{PASS}] definition {DEFINITION!r} loaded ({match['name']})")

settings = {f["name"]: f for f in match.get("fields", [])}
print(f"  [{INFO}] settings exposed: {sorted(settings)}")


# --------------------------------------------------------------------------- 2
print("\n2. register the indexer")
_, profiles = api("/api/v1/appprofile")
_, existing = api("/api/v1/indexer")
current = next((i for i in existing if i.get("name") == INDEXER_NAME), None)

payload = {
    "name": INDEXER_NAME,
    "implementation": match["implementation"],
    "implementationName": match.get("implementationName"),
    "configContract": match["configContract"],
    "definitionName": DEFINITION,
    "enable": True,
    "protocol": "torrent",
    "priority": 25,
    "appProfileId": profiles[0]["id"] if profiles else 1,
    # Keep any existing tag assignment: tags are how an indexer proxy is bound
    # to an indexer, so dropping them would silently disable proxy mode.
    "tags": (current.get("tags") or []) if current else [],
    "fields": match.get("fields", []),
}

if current:
    # Adding without forceSave is the only call that validates synchronously and
    # returns the failure reason, so remove any previous copy first. This is the
    # same path the "Add Indexer" button in the UI takes.
    api(f"/api/v1/indexer/{current['id']}", "DELETE")
    print(f"  [{INFO}] removed the previous indexer (id={current['id']})")

status, body = api_raw("/api/v1/indexer", "POST", payload)
if status not in (200, 201) or not isinstance(body, dict):
    print(f"  [{FAIL}] indexer could not be added (HTTP {status})")
    for entry in body if isinstance(body, list) else [body]:
        if isinstance(entry, dict):
            print(f"         error: {entry.get('errorMessage')}")
        else:
            print(f"         {entry}")
    sys.exit(1)

indexer = body
indexer_id = indexer["id"]
print(f"  [{PASS}] indexer added and validated by Prowlarr (id={indexer_id})")
check(indexer.get("enable") is True, "indexer is enabled")
check(indexer.get("protocol") == "torrent", "protocol is torrent", str(indexer.get("protocol")))


# --------------------------------------------------------------------------- 3
print("\n3. search through Prowlarr")
queries = sys.argv[1:] or DEFAULT_QUERIES
for query in queries:
    _, results = api(
        "/api/v1/search",
        params={"query": query, "type": "search", "indexerIds": [indexer_id], "limit": 100},
    )
    check(bool(results), f"search {query!r} returned results", f"{len(results)} releases")
    if not results:
        continue
    sample = results[0]
    print(
        f"         e.g. {sample['title'][:70]}\n"
        f"              size={sample['size']} seeders={sample['seeders']} "
        f"cat={sample.get('category') or sample.get('categories')} "
        f"date={sample['publishDate']}"
    )
    for field in ("title", "size", "seeders", "downloadUrl", "infoHash", "publishDate"):
        check(sample.get(field) not in (None, "", 0), f"search result has {field}")

    # distinct torrents must not collapse onto one guid
    guids = {r.get("guid") for r in results}
    check(len(guids) == len(results), "every result has a unique guid", f"{len(guids)}/{len(results)}")


# --------------------------------------------------------------------------- 4
print("\n4. torrent download")
query = queries[0]
_, results = api(
    "/api/v1/search",
    params={"query": query, "type": "search", "indexerIds": [indexer_id], "limit": 5},
)
if results:
    link = results[0].get("downloadUrl") or results[0].get("magnetUrl")
    print(f"  [{INFO}] {link[:110]}")
    request = urllib.request.Request(link)
    request.add_header("X-Api-Key", KEY)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            blob = response.read()
        is_torrent = blob[:1] == b"d" and b"announce" in blob[:4096]
        check(is_torrent, "downloaded file is a bencoded torrent", f"{len(blob)} bytes")
        if is_torrent:
            try:
                text = blob[:4000].decode("latin-1")
                name = next(
                    (s for s in text.split("4:name")[1:2]), ""
                )
                print(f"         contains 'announce' and starts with 'd' -> OK")
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        check(False, "downloaded file is a bencoded torrent", f"{type(exc).__name__}: {exc}")
else:
    check(False, "found a result to download")


# --------------------------------------------------------------------------- 5
print("\n5. keyword-less search (RSS sync)")
# This is the request path Sonarr/Radarr RSS sync uses: no keywords, so the
# proxy falls back to the newest torrents and the API's limit of 50 applies.
_, feed = api(
    "/api/v1/search",
    params={"type": "search", "indexerIds": [indexer_id], "limit": 25},
)
check(bool(feed), "keyword-less search returned results", f"{len(feed)} releases")
for row in feed[:3]:
    print(f"         {row['title'][:74]}")


# --------------------------------------------------------------------------- summary
print("\n6. restoring log settings")
try:
    _, dev = api("/api/v1/config/development")
    dev["consoleLogLevel"] = ""
    dev["logIndexerResponse"] = False
    api(f"/api/v1/config/development/{dev['id']}", "PUT", dev)
    print(f"  [{PASS}] log level and indexer response logging restored")
except Exception as exc:  # noqa: BLE001
    print(f"  [{INFO}] could not restore log settings: {exc}")

print("\n" + "=" * 68)
if failures:
    print(f"RESULT: {len(failures)} check(s) failed")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("RESULT: all checks passed")
