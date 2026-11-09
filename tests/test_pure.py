"""Offline tests for the pure helpers in proxy/app.py (no network)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

import app  # noqa: E402


class SizeBytes(unittest.TestCase):
    def test_numbers_and_text(self):
        self.assertEqual(app.size_bytes(1234), 1234)
        self.assertEqual(app.size_bytes({"value": 10}), 10)
        self.assertEqual(app.size_bytes("1.5 GB"), int(1.5 * 1024 ** 3))
        self.assertEqual(app.size_bytes("700MiB"), 700 * 1024 ** 2)

    def test_garbage(self):
        for value in (None, True, "", "n/a"):
            self.assertEqual(app.size_bytes(value), 0)


class Ordering(unittest.TestCase):
    torrents = [
        {"id": 2, "hash": "b", "size": 100},
        {"id": 1, "hash": "a", "size": 300},
        {"id": 3, "hash": "c", "size": 100},
    ]

    def test_largest_first_id_breaks_ties(self):
        self.assertEqual([t["id"] for t in app.ordered_torrents(self.torrents)], [1, 2, 3])

    def test_order_by_hash(self):
        self.assertEqual(app.torrent_order({"hash": "c"}, self.torrents), 3)
        self.assertIsNone(app.torrent_order({"hash": "zz"}, self.torrents))


class DownloadUrl(unittest.TestCase):
    def test_stable_route(self):
        url = app.download_url("honzuki", 1)
        self.assertTrue(url.startswith("http://"))
        self.assertTrue(url.endswith(f"{app.DIVERT_PREFIX}/torrent/honzuki/1.torrent"))
        path = url.split("://", 1)[1].split("/", 1)[1]
        self.assertIsNotNone(app.TORRENT_ROUTE.match("/" + path))

    def test_hash_fallback(self):
        self.assertTrue(app.download_url(None, None, "abc").endswith("/anime/torrents/abc/file"))
        self.assertEqual(app.download_url(None, None), "")


class ParseQuery(unittest.TestCase):
    def test_defaults_and_clamp(self):
        self.assertEqual(app.parse_search_query({}), ("", 100))
        self.assertEqual(app.parse_search_query({"q": ["naruto"], "limit": ["0"]}), ("naruto", 1))
        self.assertEqual(app.parse_search_query({"query": ["x"], "limit": ["bad"]}), ("x", 100))
        self.assertEqual(app.parse_search_query({"limit": ["99999"]})[1], app.MAX_ROWS)


if __name__ == "__main__":
    unittest.main()
