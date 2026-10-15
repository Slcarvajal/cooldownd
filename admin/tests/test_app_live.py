import http.client
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import tests  # isolation first
import app
from modules import night, remote
from modules.common import STATE

assert tests.HOME  # the isolation ran: temp HOME, no network, no commands


class Card:
    NAME, TITLE, TAB = "fake", "Fake", "home"

    @staticmethod
    def card(cfg):
        return "<p>fake</p>"


class LivePayload(unittest.TestCase):
    def setUp(self):
        for k in ("agents", "notices"):
            self.addCleanup(STATE.pop, k, None)

    def test_quiet_and_notices(self):
        now = time.time()
        STATE["agents"] = {"items": [{"handle": "t1", "title": "Fix", "project": "api-server", "summary": "Go?",
                                      "status": "waiting"}]}
        STATE["notices"] = [{"id": "desk:sit:1", "text": "Stand up", "until": now + 60},
                            {"id": "desk:sit:0", "text": "Old", "until": now - 1}]
        remote_notes = [{"id": "desk:sit:1", "text": "Stand up", "until": now + 60},
                        {"id": "desk:nudge:5", "text": "Busy", "until": now + 60}]
        with mock.patch.object(night, "quiet", return_value=True), \
                mock.patch.object(remote, "notices", return_value=remote_notes), \
                mock.patch.object(remote, "pager", return_value=[]):
            d = app.live_payload({"modules": ["fake"]}, [Card])
        self.assertIs(d["quiet"], True)
        self.assertEqual([n["id"] for n in d["notices"]], ["desk:sit:1", "desk:nudge:5"])
        self.assertEqual(d["cards"], {"fake": "<p>fake</p>"})
        self.assertEqual(d["waiting"], 1)
        json.dumps(d)

    def test_without_night_or_notices(self):
        d = app.live_payload({"modules": []}, [])
        self.assertIs(d["quiet"], False)
        self.assertEqual(d["notices"], [])


class HostAllowed(unittest.TestCase):
    def test_hosts(self):
        cfg = {"hosts": ["hub.lan"]}
        for h in ("127.0.0.1:8095", "localhost", "[::1]:80", "pi.tail.ts.net:8444", "hub.lan:1"):
            self.assertTrue(app.host_allowed(h, cfg), h)
        for h in ("evil.example", "evil.example:8095", "", None, "ts.net.evil.example"):
            self.assertFalse(app.host_allowed(h, cfg), h)


class Share(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"modules": ["coop"], "allowed_users": []}, f)
        self.addCleanup(os.remove, path)
        p = mock.patch.object(app, "CONFIG_PATH", path)
        p.start()
        self.addCleanup(p.stop)
        self.calls, self.clients = [], []
        fake = types.ModuleType("modules.coop")

        def serve(cfg, auth, client=""):
            self.calls.append(auth)
            self.clients.append(client)
            if auth == "Bearer good":
                return 200, b'{"v": 1}'
            return 401, b'{"error": "Unknown share token."}'

        fake.serve = serve
        p = mock.patch.dict(sys.modules, {"modules.coop": fake})
        p.start()
        self.addCleanup(p.stop)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.ShareHandler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def ask(self, method, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=5)
        with mock.patch("sys.stdout"):  # the handler's access log
            c.request(method, path, headers=headers or {})
            r = c.getresponse()
            body = r.read()
        c.close()
        return r, body

    def check_headers(self, r):
        self.assertTrue(r.getheader("Content-Type").startswith("application/json"))
        self.assertEqual(r.getheader("Cache-Control"), "no-store")

    def test_week_with_and_without_token(self):
        r, body = self.ask("GET", "/share/v1/week", {"Authorization": "Bearer good"})
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(body), {"v": 1})
        self.check_headers(r)
        r, body = self.ask("GET", "/share/v1/week")
        self.assertEqual(r.status, 401)
        self.check_headers(r)
        self.assertEqual(self.calls, ["Bearer good", ""])

    def test_client_key_for_the_rate_limit(self):
        self.ask("GET", "/share/v1/week", {"Authorization": "Bearer x", "Tailscale-User-Login": "ana@example.com"})
        self.ask("GET", "/share/v1/week", {"Authorization": "Bearer x"})
        self.assertEqual(self.clients, ["ana@example.com", "127.0.0.1"])

    def test_other_paths_are_404(self):
        for path in ("/", "/live", "/share/v1/week/x", "/file/coop/x/y", "/page/life/game"):
            r, _ = self.ask("GET", path)
            self.assertEqual(r.status, 404, path)
            self.check_headers(r)
        self.assertEqual(self.calls, [])

    def test_other_methods_are_405(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "BREW"):
            r, _ = self.ask(method, "/share/v1/week", {"Authorization": "Bearer good"})
            self.assertEqual(r.status, 405, method)
            self.check_headers(r)
        self.assertEqual(self.calls, [])

    def test_foreign_host_is_refused(self):
        r, _ = self.ask("GET", "/share/v1/week", {"Host": "evil.example", "Authorization": "Bearer good"})
        self.assertEqual(r.status, 403)
        self.check_headers(r)
        self.assertEqual(self.calls, [])

    def test_log_has_no_headers_or_query(self):
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=5)
            c.request("GET", "/share/v1/week?token=secret123", headers={"Authorization": "Bearer good"})
            c.getresponse().read()
            c.close()
        log = "\n".join(out)
        self.assertNotIn("secret123", log)
        self.assertNotIn("good", log)

    def test_not_a_panel_handler(self):
        self.assertFalse(issubclass(app.ShareHandler, app.Handler))


if __name__ == "__main__":
    unittest.main()
