import json
import time
import unittest
from unittest import mock

import tests  # isolation first
from modules import remote

assert tests.HOME  # the isolation ran: temp HOME, no network, no commands

CFG = {"modules": ["desk.agents", "pi.life", "power"],
       "remotes": {"desk": {"url": "https://desk.example.ts.net:8443"}, "pi": {"url": "https://pi.example.ts.net:8443"},
                   "spare": {"url": "https://spare.example.ts.net:8443"}}}


class Notices(unittest.TestCase):
    def setUp(self):
        remote._live.clear()
        self.addCleanup(remote._live.clear)
        until = time.time() + 600
        nudge = {"id": "desk:nudge:100", "text": "Your agents are busy.", "until": until}
        self.answers = {
            "desk": {"cards": {"agents": "<p>a</p>"}, "pager": [], "notices": [nudge]},
            # the Pi passes the desk's notice through, plus junk that must be dropped
            "pi": {"cards": {"life": "<p>l</p>"}, "pager": [],
                   "notices": [dict(nudge), {"id": "desk:sit:7", "text": "Stand up", "until": until},
                               {"id": 3, "text": "bad id", "until": until}, "junk", {"id": "x", "text": "no until"}]},
            "spare": {"cards": {}, "pager": [], "notices": [{"id": "spare:1", "text": "unused", "until": until}]},
        }
        self.asked = []

        def request(cfg, rid, path, body=None, timeout=remote.TIMEOUT):
            self.asked.append((rid, path))
            return json.dumps(self.answers[rid]).encode()

        p = mock.patch.object(remote, "_request", request)
        p.start()
        self.addCleanup(p.stop)

    def test_dedupes_between_desk_and_pi(self):
        notes = remote.notices(CFG)
        self.assertEqual([n["id"] for n in notes], ["desk:nudge:100", "desk:sit:7"])
        self.assertEqual(set(notes[0]), {"id", "text", "until"})

    def test_only_remotes_in_use_and_one_fetch(self):
        remote.live(CFG, "desk")
        remote.notices(CFG)
        self.assertEqual(sorted(self.asked), [("desk", "live"), ("pi", "live")])

    def test_live_signature_unchanged(self):
        cards, pager = remote.live(CFG, "desk")
        self.assertEqual(cards, {"agents": "<p>a</p>"})
        self.assertEqual(pager, [])

    def test_unreachable_remote_has_no_notices(self):
        def down(cfg, rid, path, body=None, timeout=remote.TIMEOUT):
            raise OSError("down")

        with mock.patch.object(remote, "_request", down):
            self.assertEqual(remote.notices(CFG), [])
            self.assertEqual(remote.live(CFG, "desk"), (None, []))


if __name__ == "__main__":
    unittest.main()
