import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

import modules
from modules import coop
from modules.common import STATE

GOOD = {"v": 1, "name": "Ana", "level": 7, "title": "Athlete", "week": "2026-W42",
        "quests": [{"name": "Gym 3×", "have": 2, "need": 3, "done": False}],
        "boss": {"name": "Sloth", "hp": 1, "max": 3, "defeated": False}}


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for name, attr in (("share-tokens.json", "TOKENS_PATH"), ("friends.json", "FRIENDS_PATH")):
            p = mock.patch.object(coop, attr, os.path.join(self.dir, "cfg", name))
            p.start()
            self.addCleanup(p.stop)
        coop._fails.clear()
        coop._client_fails.clear()
        coop._log.update(read=None, errors={}, unsafe={})

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = coop.main(list(args))
        return code, out.getvalue()


class Cli(Files):
    def test_add_prints_once_and_stores_only_the_hash(self):
        code, out = self.cli("add", "ana")
        self.assertEqual(code, 0)
        token = out.strip().splitlines()[-1]
        self.assertGreaterEqual(len(token), 40)
        self.assertEqual(stat.S_IMODE(os.stat(coop.TOKENS_PATH).st_mode), 0o600)
        with open(coop.TOKENS_PATH) as f:
            raw = f.read()
        self.assertNotIn(token, raw)
        self.assertEqual(json.loads(raw), {"ana": coop._hash(token)})
        self.assertEqual(self.cli("list"), (0, "ana\n"))

    def test_bad_friend_name(self):
        self.assertEqual(self.cli("add", "../x")[0], 2)
        self.assertEqual(self.cli("revoke", "nobody")[0], 1)
        self.assertFalse(os.path.exists(coop.TOKENS_PATH))


class Serve(Files):
    def setUp(self):
        super().setUp()
        self.token = self.cli("add", "ana")[1].strip().splitlines()[-1]
        p = mock.patch.object(coop, "summary", return_value=dict(GOOD))
        p.start()
        self.addCleanup(p.stop)

    def test_right_token(self):
        status, body = coop.serve({}, f"Bearer {self.token}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), GOOD)

    def test_wrong_missing_and_revoked(self):
        for i, auth in enumerate(("Bearer nope", "", self.token, f"Basic {self.token}", "Bearer " + "x" * 5000)):
            self.assertEqual(coop.serve({}, auth, f"c{i}")[0], 401, auth[:20])
        self.cli("revoke", "ana")
        self.assertEqual(coop.serve({}, f"Bearer {self.token}", "c9")[0], 401)

    def test_global_429(self):
        for i in range(coop.FAIL_LIMIT):
            self.assertEqual(coop.serve({}, "Bearer nope", f"node{i}")[0], 401)
        self.assertEqual(coop.serve({}, f"Bearer {self.token}", "ana")[0], 429)  # global: even a good token waits
        with mock.patch("time.monotonic", return_value=coop._fails[-1] + coop.FAIL_WINDOW + 1):
            self.assertEqual(coop.serve({}, f"Bearer {self.token}", "ana")[0], 200)

    def test_one_noisy_client_only_locks_itself_out(self):
        for _ in range(coop.CLIENT_FAIL_LIMIT):
            self.assertEqual(coop.serve({}, "Bearer nope", "noisy@example.com")[0], 401)
        self.assertEqual(coop.serve({}, f"Bearer {self.token}", "noisy@example.com")[0], 429)
        self.assertEqual(coop.serve({}, "Bearer nope", "noisy@example.com")[0], 429)  # a 429 adds no failure
        self.assertEqual(len(coop._fails), coop.CLIENT_FAIL_LIMIT)
        self.assertEqual(coop.serve({}, f"Bearer {self.token}", "ana@example.com")[0], 200)
        self.assertEqual(coop.serve({}, f"Bearer {self.token}", "127.0.0.1")[0], 200)
        with mock.patch("time.monotonic", return_value=coop._fails[-1] + coop.FAIL_WINDOW + 1):
            self.assertEqual(coop.serve({}, f"Bearer {self.token}", "noisy@example.com")[0], 200)
        self.assertEqual(coop._client_fails, {})  # forgotten once outside the window

    def test_open_token_file_is_refused(self):
        os.chmod(coop.TOKENS_PATH, 0o644)
        status, body = coop.serve({}, f"Bearer {self.token}", "ana")
        self.assertEqual(status, 503)
        self.assertNotIn("share-tokens", body.decode())
        self.assertIn("share-tokens.json can be read or written by others", coop.card({}))
        os.chmod(coop.TOKENS_PATH, 0o600)
        self.assertEqual(coop.serve({}, f"Bearer {self.token}", "ana")[0], 200)
        self.assertNotIn("by others", coop.card({}))


class Summary(unittest.TestCase):
    def test_only_the_whitelist_leaves(self):
        game = {"name": "Alex", "level": 9, "title": "Veteran", "xp": 1234,
                "quests": [{"name": "Gym", "reward": "+100", "have": 3, "need": 3, "done": True}],
                "boss": {"name": "Procrastinator", "emoji": "👾", "hp": 0, "max": 3, "defeated": True, "week": "x"},
                "today": {"meals": [{"title": "Oatmeal"}], "kcal": 900}, "sleep": {"debt_min": 90, "last": 400},
                "party": [{"title": "Fix client X", "agent": "claude"}], "feed": [{"text": "Meal: oatmeal"}],
                "bars": {"hp": 50}, "avatar": "<svg/>"}
        fake = types.SimpleNamespace(game=lambda cfg: game)
        with mock.patch.dict(sys.modules, {"modules.life": fake}), mock.patch.object(modules, "life", fake, create=True):
            s = coop.summary({})
        self.assertEqual(set(s), {"v", "name", "level", "title", "week", "quests", "boss"})
        self.assertEqual(s["quests"], [{"name": "Gym", "have": 3, "need": 3, "done": True}])
        self.assertEqual(s["boss"], {"name": "Procrastinator", "hp": 0, "max": 3, "defeated": True})
        text = json.dumps(s).lower()
        for word in ("meal", "oatmeal", "sleep", "debt", "agent", "claude", "client", "kcal", "svg"):
            self.assertNotIn(word, text)


class Clean(unittest.TestCase):
    def test_good(self):
        self.assertEqual(coop.clean(GOOD), GOOD)

    def test_garbage(self):
        for bad in (None, [], "x", {**GOOD, "v": 2}, {**GOOD, "level": "7"}, {**GOOD, "level": 5000},
                    {**GOOD, "level": True}, {**GOOD, "name": 5}, {**GOOD, "name": "<>"}, {**GOOD, "week": "soon"},
                    {**GOOD, "title": None}):
            self.assertIsNone(coop.clean(bad), bad)

    def test_trims_and_escapes(self):
        d = coop.clean({**GOOD, "name": '<img src=x onerror="alert(1)">Bob\x00‮', "extra": "drop me",
                        "quests": [{"name": "q", "have": 1, "need": 2, "done": False, "x": 1}] * 12
                        + [{"name": "bad", "have": -1, "need": 2, "done": False}],
                        "boss": {"name": "B", "hp": "1", "max": 3, "defeated": False}})
        self.assertNotIn("extra", d)
        for ch in '<>"\x00‮':
            self.assertNotIn(ch, d["name"])
        self.assertTrue(d["name"].endswith("Bob"))
        self.assertLessEqual(len(coop.clean({**GOOD, "name": "n" * 500})["name"]), coop.TEXT_MAX)
        self.assertEqual(len(d["quests"]), coop.QUESTS_MAX)
        self.assertEqual(d["quests"][0], {"name": "q", "have": 1, "need": 2, "done": False})
        self.assertIsNone(d["boss"])


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def read(self, n=-1):
        return self.body if n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Fetch(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.body = json.dumps(GOOD).encode()
        test = self

        class Opener:
            def open(self, req, timeout=None):
                test.requests.append((req, timeout))
                return FakeResponse(test.body)

        p = mock.patch.object(coop, "_opener", return_value=Opener())
        p.start()
        self.addCleanup(p.stop)
        os.makedirs(os.path.dirname(modules.common.TOKEN_PATH), exist_ok=True)
        with open(modules.common.TOKEN_PATH, "w") as f:
            f.write("HUB-SECRET")
        self.addCleanup(os.remove, modules.common.TOKEN_PATH)

    def test_friend_gets_only_their_token(self):
        self.assertEqual(coop.fetch("https://ana-pi.tail123.ts.net:8444", "friend-token"), GOOD)
        req, timeout = self.requests[0]
        self.assertEqual(req.full_url, "https://ana-pi.tail123.ts.net:8444/share/v1/week")
        self.assertEqual(timeout, coop.TIMEOUT)
        self.assertEqual(req.get_header("Authorization"), "Bearer friend-token")
        self.assertIsNone(req.get_header("X-hub-token"))
        self.assertNotIn("HUB-SECRET", json.dumps(dict(req.header_items())))

    def test_bad_urls_never_connect(self):
        for url in ("http://ana.tail1.ts.net", "https://evil.example.com", "https://127.0.0.1", "https://ts.net",
                    "https://ana.ts.net.evil.com", "https://me@ana.tail1.ts.net", "ftp://ana.ts.net", "", None,
                    "https://evil.com\\.ts.net", "https://evil.com\\@ana.ts.net", "https://evil.com%2f.ts.net",
                    "https://me:pw@ana.tail1.ts.net", "https://ana.tail1.ts.net:0", "https://ana.tail1.ts.net:65536",
                    "https://ana.tail1.ts.net:+443", "https://ana.tail1.ts.net:", "https://ana_x.tail1.ts.net",
                    "https://ana..ts.net", "https://.ts.net", "https://ana.tail1.ts.net /x", "https://[::1].ts.net",
                    "https://ana.tail1.ts.net\\x", "https://ana.tail1.ts.net\n.evil.com", "https://ánä.ts.net"):
            with self.assertRaises(ValueError, msg=url):
                coop.fetch(url, "t")
        for token in ("bad token", "x\r\nX-Hub-Token: y", "", None):
            with self.assertRaises(ValueError):
                coop.fetch("https://ana.tail1.ts.net", token)
        self.assertEqual(self.requests, [])

    def test_good_urls(self):
        for url, want in (("https://ana.tail1.ts.net", "https://ana.tail1.ts.net/share/v1/week"),
                          ("HTTPS://Ana-PI.Tail1.TS.NET:8444/", "https://ana-pi.tail1.ts.net:8444/share/v1/week"),
                          ("https://ana.tail1.ts.net:1/anything?x=1", "https://ana.tail1.ts.net:1/share/v1/week"),
                          ("https://ana.tail1.ts.net:65535", "https://ana.tail1.ts.net:65535/share/v1/week")):
            self.assertEqual(coop.friend_url(url), want)

    def test_too_big_or_bad_answer(self):
        self.body = b" " * coop.MAX_BYTES + json.dumps(GOOD).encode()
        with self.assertRaises(ValueError):
            coop.fetch("https://ana.tail1.ts.net", "t")
        self.body = json.dumps({**GOOD, "level": "max"}).encode()
        with self.assertRaises(ValueError):
            coop.fetch("https://ana.tail1.ts.net", "t")


class Redirects(unittest.TestCase):
    def test_opener_refuses_redirects_and_proxies(self):
        o = coop._opener()
        kinds = [type(h) for h in o.handlers]
        self.assertIn(coop._NoRedirect, kinds)
        self.assertNotIn(urllib.request.HTTPRedirectHandler, kinds)
        # ProxyHandler({}) replaces the default one and adds nothing: no proxy from the environment
        self.assertFalse([h for h in o.handlers if isinstance(h, urllib.request.ProxyHandler)])
        req = urllib.request.Request("https://ana.tail1.ts.net/share/v1/week")
        with self.assertRaises(urllib.error.HTTPError):
            coop._NoRedirect().redirect_request(req, io.BytesIO(), 302, "Found", {}, "http://127.0.0.1:8095/live")


class Board(Files):
    def test_board_has_me_and_good_friends(self):
        os.makedirs(os.path.dirname(coop.FRIENDS_PATH), exist_ok=True)
        with open(coop.FRIENDS_PATH, "w") as f:
            json.dump({"Ana": {"url": "https://ana.tail1.ts.net", "token": "t"}, "Eve": {"url": "http://x", "token": "t"},
                       "Bob": "junk"}, f)
        os.chmod(coop.FRIENDS_PATH, 0o600)
        me = {**GOOD, "name": "Alex", "quests": [{"name": "Gym", "have": 3, "need": 3, "done": True}]}
        with mock.patch.object(coop, "summary", return_value=me), \
                mock.patch.object(coop, "fetch", side_effect=lambda url, token: coop.friend_url(url) and GOOD):
            coop.sample({"life": {"coop": {"board": "Quest <board>", "party_goal": 12}}})
        c = STATE.pop("coop")
        self.assertEqual(c["board"], "Quest board")
        self.assertEqual((c["goal"], c["total"]), (12, 1))
        self.assertEqual([(r["name"], r["me"], r["done"], r["need"]) for r in c["rows"]],
                         [("Alex", True, 1, 1), ("Ana", False, 0, 1)])
        self.assertEqual(sorted(coop._log["errors"]), ["Bob", "Eve"])
        self.assertIn("Eve", coop.card({}))

    def test_open_friends_file_is_refused(self):
        os.makedirs(os.path.dirname(coop.FRIENDS_PATH), exist_ok=True)
        with open(coop.FRIENDS_PATH, "w") as f:
            json.dump({"Ana": {"url": "https://ana.tail1.ts.net", "token": "t"}}, f)
        for mode in (0o640, 0o604, 0o660):
            os.chmod(coop.FRIENDS_PATH, mode)
            STATE["coop"] = {"stale": True}
            with mock.patch.object(coop, "fetch") as fetch, mock.patch.object(coop, "summary", return_value=GOOD):
                coop.sample({})
            fetch.assert_not_called()
            self.assertNotIn("coop", STATE)
            self.assertIn("friends.json can be read or written by others", coop.card({}))

    def test_no_friends_no_board(self):
        STATE["coop"] = {"stale": True}
        coop.sample({})
        self.assertNotIn("coop", STATE)


if __name__ == "__main__":
    unittest.main()
