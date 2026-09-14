#!/usr/bin/env python3
"""AniLibria -> Prowlarr bridge.

AniLibria's public API needs *two* round trips to turn a keyword search into
torrents:

    1. GET /app/search/releases?query=...   -> releases (no torrents attached)
    2. GET /anime/torrents/release/{id}     -> the torrents of one release

Prowlarr's Cardigann engine, which is what backs runtime YAML definitions in
`Definitions/Custom`, can only issue a *single* request per search
(`SearchPathBlock` has no construct for chaining requests). So a pure YAML
definition cannot implement keyword search against this API.

This service performs the two-step lookup and returns one flattened JSON payload
that a Cardigann definition can consume in a single request:

    GET /search?q=<terms>&limit=<n>

    {
      "rows": [
        {
          "release": { "id": .., "alias": "..", "type": "TV", "year": ..,
                       "name_main": "..", "name_english": "..", "poster": "..", ... },
          "torrent": { "hash": "..", "label": "..", "size": .., "seeders": ..,
                       "leechers": .., "grabs": .., "quality": "1080p", ... }
        },
        ...
      ]
    }

An empty `q` returns the newest torrents, which is what Prowlarr uses for
indexer tests and for RSS sync.

Also served:

    GET /healthz    liveness probe
    GET /           short description of the endpoints

Everything else (categories, titles, download URLs, date formatting) is left to
the YAML definition, so the indexing logic stays in the Prowlarr config.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_BASE = os.environ.get("ANILIBRIA_API_BASE", "https://aniliberty.top/api/v1").rstrip("/")
SITE = os.environ.get("ANILIBRIA_SITE", "https://aniliberty.top").rstrip("/")
PORT = int(os.environ.get("BRIDGE_PORT", "8787"))
BIND = os.environ.get("BRIDGE_BIND", "0.0.0.0")

HTTP_TIMEOUT = float(os.environ.get("BRIDGE_HTTP_TIMEOUT", "30"))
# The API rejects limit > 50 on /anime/torrents with HTTP 422.
API_MAX_LIMIT = int(os.environ.get("BRIDGE_API_MAX_LIMIT", "50"))
# How many releases to resolve into torrents for one keyword search.
MAX_RELEASES = int(os.environ.get("BRIDGE_MAX_RELEASES", "10"))
# Politeness delay between the per-release requests.
REQUEST_DELAY = float(os.environ.get("BRIDGE_REQUEST_DELAY", "0.2"))
# Number of upstream retries for transient failures.
RETRIES = int(os.environ.get("BRIDGE_RETRIES", "3"))
CACHE_TTL = float(os.environ.get("BRIDGE_CACHE_TTL", "300"))
MAX_ROWS = int(os.environ.get("BRIDGE_MAX_ROWS", "500"))

USER_AGENT = os.environ.get(
    "BRIDGE_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

_cache: dict[tuple[str, int], tuple[float, list]] = {}
_cache_lock = threading.Lock()
_log_lock = threading.Lock()


def log(message: str) -> None:
    with _log_lock:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def api_get(path: str, params: dict | None = None) -> object:
    """GET a JSON document from the AniLibria API, retrying transient failures."""
    url = f"{API_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    last_error: Exception | None = None
    for attempt in range(RETRIES):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (408, 425, 429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - network layer, retry then surface
            last_error = exc
            if attempt < RETRIES - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise

    raise RuntimeError(f"request failed: {url}") from last_error


def _scalar(value: object) -> object:
    """AniLibria wraps many values as {"value": .., "description": ..}."""
    if isinstance(value, dict):
        return value.get("value")
    return value


def _as_list(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    return []


def project_release(release: object) -> dict:
    """Flatten a release object so the YAML only needs single-level selectors."""
    release = release if isinstance(release, dict) else {}
    name = release.get("name") if isinstance(release.get("name"), dict) else {}
    season = release.get("season") if isinstance(release.get("season"), dict) else {}
    poster = release.get("poster") if isinstance(release.get("poster"), dict) else {}

    return {
        "id": release.get("id"),
        "alias": release.get("alias"),
        "type": _scalar(release.get("type")),
        "type_description": (release.get("type") or {}).get("description")
        if isinstance(release.get("type"), dict)
        else None,
        "year": release.get("year"),
        "name_main": name.get("main"),
        "name_english": name.get("english"),
        "name_alternative": name.get("alternative"),
        "season": _scalar(release.get("season")),
        "season_description": season.get("description"),
        "episodes_total": release.get("episodes_total"),
        "poster": poster.get("src"),
        "description": release.get("description"),
    }


def project_torrent(torrent: object) -> dict:
    """Flatten a torrent object."""
    torrent = torrent if isinstance(torrent, dict) else {}
    return {
        "id": torrent.get("id"),
        "hash": torrent.get("hash"),
        "label": torrent.get("label"),
        "filename": torrent.get("filename"),
        "size": torrent.get("size"),
        "seeders": torrent.get("seeders"),
        "leechers": torrent.get("leechers"),
        "grabs": torrent.get("completed_times"),
        "created_at": torrent.get("created_at"),
        "updated_at": torrent.get("updated_at"),
        "episodes": torrent.get("description"),
        "quality": _scalar(torrent.get("quality")),
        "codec": _scalar(torrent.get("codec")),
        "color": _scalar(torrent.get("color")),
        "source": _scalar(torrent.get("type")),
        "magnet": torrent.get("magnet"),
    }


def make_row(release: object, torrent: object) -> dict:
    return {"release": project_release(release), "torrent": project_torrent(torrent)}


def latest_torrents(limit: int) -> list:
    """Newest torrents. /anime/torrents rejects limit > 50, so clamp and back off."""
    for candidate in (min(limit, API_MAX_LIMIT), 25, 10):
        candidate = max(1, candidate)
        try:
            payload = api_get("/anime/torrents", {"limit": candidate})
            return _as_list(payload)
        except urllib.error.HTTPError as exc:
            if exc.code == 422 and candidate > 10:
                log(f"  limit={candidate} rejected by the API, retrying smaller")
                continue
            raise
    return []


def search(keywords: str, limit: int) -> list[dict]:
    """Return flattened rows for a keyword search, or the newest torrents if empty."""
    keywords = (keywords or "").strip()
    rows: list[dict] = []

    if not keywords:
        for item in latest_torrents(limit):
            if isinstance(item, dict):
                rows.append(make_row(item.get("release"), item))
        log(f"rss/latest: {len(rows)} rows (limit={min(limit, API_MAX_LIMIT)})")
    else:
        payload = api_get("/app/search/releases", {"query": keywords})
        releases = _as_list(payload)
        log(f"search {keywords!r}: {len(releases)} releases matched")

        seen_releases: set[object] = set()
        for release in releases[:MAX_RELEASES]:
            if not isinstance(release, dict):
                continue
            release_id = release.get("id")
            if release_id is None or release_id in seen_releases:
                continue
            seen_releases.add(release_id)

            if rows:
                time.sleep(REQUEST_DELAY)

            try:
                torrent_payload = api_get(f"/anime/torrents/release/{release_id}")
            except Exception as exc:  # noqa: BLE001 - skip a bad release, keep going
                log(f"  release {release_id}: {type(exc).__name__}: {exc}")
                continue

            for torrent in _as_list(torrent_payload):
                if isinstance(torrent, dict):
                    rows.append(make_row(torrent.get("release") or release, torrent))

        log(f"search {keywords!r}: {len(rows)} torrents from {len(seen_releases)} releases")

    # De-duplicate by infohash, preserving order.
    unique: list[dict] = []
    seen_hashes: set[object] = set()
    for row in rows:
        infohash = row["torrent"].get("hash")
        if infohash and infohash in seen_hashes:
            continue
        if infohash:
            seen_hashes.add(infohash)
        unique.append(row)

    return unique[:MAX_ROWS]


def cached_search(keywords: str, limit: int) -> tuple[list[dict], bool]:
    key = (keywords.strip().lower(), limit)
    now = time.time()

    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1], True

    rows = search(keywords, limit)

    with _cache_lock:
        if CACHE_TTL > 0:
            _cache[key] = (now + CACHE_TTL, rows)
        if len(_cache) > 256:
            for stale in [k for k, v in _cache.items() if v[0] <= now]:
                _cache.pop(stale, None)

    return rows, False


class Handler(BaseHTTPRequestHandler):
    server_version = "AniLibriaBridge/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/healthz":
            self._send(200, {"ok": True, "api_base": API_BASE})
            return

        if parsed.path == "/":
            self._send(
                200,
                {
                    "service": "anilibria-bridge",
                    "endpoints": {
                        "/search": "?q=<terms>&limit=<n> - flattened AniLibria results",
                        "/healthz": "liveness probe",
                    },
                    "api_base": API_BASE,
                    "_links": {
                        "search": f"{SITE}/api/v1/app/search/releases?query={{q}}",
                        "torrent_file": f"{SITE}/api/v1/anime/torrents/{{hash}}/file",
                    },
                },
            )
            return

        if parsed.path != "/search":
            self._send(404, {"error": "not found", "path": parsed.path})
            return

        keywords = (query.get("q") or query.get("query") or [""])[0]
        try:
            limit = int((query.get("limit") or ["100"])[0])
        except ValueError:
            limit = 100
        limit = max(1, min(limit, MAX_ROWS))

        try:
            rows, from_cache = cached_search(keywords, limit)
        except Exception as exc:  # noqa: BLE001 - report upstream failure to Prowlarr
            log(f"ERROR q={keywords!r}: {type(exc).__name__}: {exc}")
            self._send(
                502,
                {
                    "error": "upstream request failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "query": keywords,
                },
            )
            return

        self._send(
            200,
            {
                "rows": rows,
                "meta": {
                    "query": keywords,
                    "count": len(rows),
                    "cached": from_cache,
                    "api_base": API_BASE,
                },
            },
        )

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        log(f"{self.address_string()} {fmt % args}")


def main() -> None:
    log(f"anilibria-bridge listening on {BIND}:{PORT} (api={API_BASE})")
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
