import email.message
import io
import os
import unittest
import urllib.error
import urllib.request
import urllib.response
from unittest import mock

import modules
import tests  # isolation first
from modules import remote

assert tests.HOME  # the isolation ran: temp HOME, no network, no commands

CFG = {"remotes": {"desk": {"url": "https://desk.example.ts.net:8443"}}}


class FakeHTTPS(urllib.request.BaseHandler):
    """Answers every https request without a network: the desk's URL with a 302 elsewhere, anything else 200."""
    handler_order = 100  # before the real HTTPSHandler

    def __init__(self):
        self.seen = []

    def https_open(self, req):
        self.seen.append((req.full_url, req.get_header("X-hub-token")))
        hdrs = email.message.Message()
        hdrs["Content-Type"] = "application/json"
        if req.full_url.startswith("https://desk.example.ts.net:8443/"):
            hdrs["Location"] = "https://evil.example/steal"
            r = urllib.response.addinfourl(io.BytesIO(b""), hdrs, req.full_url, 302)
            r.msg = "Found"
        else:
            r = urllib.response.addinfourl(io.BytesIO(b"{}"), hdrs, req.full_url, 200)
            r.msg = "OK"
        return r


class Fetch(unittest.TestCase):
    def setUp(self):
        os.makedirs(os.path.dirname(modules.common.TOKEN_PATH), exist_ok=True)
        with open(modules.common.TOKEN_PATH, "w") as f:
            f.write("HUB-SECRET")
        self.addCleanup(os.remove, modules.common.TOKEN_PATH)
        self.fake = FakeHTTPS()
        real = remote._opener

        def opener():
            o = real()
            o.add_handler(self.fake)
            return o

        p = mock.patch.object(remote, "_opener", opener)
        p.start()
        self.addCleanup(p.stop)

    def test_redirect_is_not_followed(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            remote._fetch(CFG, "desk", "live")
        cm.exception.close()
        self.assertEqual(cm.exception.code, 302)
        # one request, to the desk: the token never went on to the redirect's target
        self.assertEqual(self.fake.seen, [("https://desk.example.ts.net:8443/live", "HUB-SECRET")])

    def test_the_stock_opener_would_have_followed(self):
        # what the fix prevents: urllib's default redirect handler re-sends the token to the new host
        o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        o.add_handler(self.fake)
        req = urllib.request.Request("https://desk.example.ts.net:8443/live", headers={"X-Hub-Token": "HUB-SECRET"})
        o.open(req, timeout=1).close()
        self.assertEqual(self.fake.seen[-1], ("https://evil.example/steal", "HUB-SECRET"))


class Opener(unittest.TestCase):
    def test_no_proxies_and_no_redirect_handler(self):
        with mock.patch.dict(os.environ, {"https_proxy": "http://proxy.example:3128",
                                          "HTTPS_PROXY": "http://proxy.example:3128"}):
            o = remote._opener()
        kinds = [type(h) for h in o.handlers]
        self.assertIn(remote._NoRedirect, kinds)
        self.assertNotIn(urllib.request.HTTPRedirectHandler, kinds)
        self.assertFalse([h for h in o.handlers if isinstance(h, urllib.request.ProxyHandler)])


if __name__ == "__main__":
    unittest.main()
