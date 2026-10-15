"""Agent creatures: the placeholder agents.item_row writes for the page's critter() (app.KIOSK_JS), the levels
it reads from the agent log, and a plain-text check of the shared generator and its CSS."""
import os
import re
import tempfile
import unittest
from unittest import mock

import tests
import app
from modules import agenthealth, agentlog, agents

DAY = "2026-10-10"


def item(**kw):
    return {"handle": "t1", "host": "desk", "project": "api-server", "title": "Fix login", "agent": "claude",
            "status": "working", "summary": "Running tests", "age": 3.0, **kw}


def attrs(html):
    tag = re.search(r'<i class="critter[^>]*>', html).group(0)
    return dict(re.findall(r'(data-[\w-]+|class|aria-hidden)="([^"]*)"', tag))


class ItemRow(unittest.TestCase):
    def test_placeholder_replaces_the_dot(self):
        html = agents.item_row(item(status="waiting"))
        self.assertNotIn('class="dot', html)
        self.assertIn('class="agent crow s-waiting"', html)
        self.assertEqual(attrs(html), {"class": "critter warn", "data-seed": "api-server|claude", "data-status": "waiting",
                                       "data-level": "1", "data-agent": "claude", "aria-hidden": "true"})
        self.assertIn("needs you", html)  # the status text stays for screen readers

    def test_level_from_the_map(self):
        lv = {("api-server", "claude"): 4, ("other", "claude"): 9}
        self.assertEqual(attrs(agents.item_row(item(), lv))["data-level"], "4")
        self.assertEqual(attrs(agents.item_row(item(agent="codex"), lv))["data-level"], "1")

    def test_attributes_are_escaped(self):
        html = agents.item_row(item(project='x"><script>', agent="a'b&c", status="stuck"))
        self.assertNotIn("<script>", html)
        a = attrs(html)
        self.assertEqual(a["data-seed"], "x&quot;&gt;&lt;script&gt;|a&#x27;b&amp;c")
        self.assertEqual(a["data-agent"], "a&#x27;b&amp;c")
        self.assertEqual(a["class"], "critter bad")


class Levels(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=tests.HOME)
        self._orig = agentlog.DB_PATH
        agentlog.DB_PATH = os.path.join(self.dir, "agents.db")
        agentlog._made.clear()

    def tearDown(self):
        agentlog.DB_PATH = self._orig
        agentlog._made.clear()

    def task(self, project, agent):
        agentlog.w("INSERT INTO task(ts, day, handle, project, title, agent, recap) VALUES(1, ?, 'h', ?, 'T', ?, 'r')",
                   (DAY, project, agent))

    def test_levels_per_project_and_agent(self):
        for _ in range(4):
            self.task("api-server", "claude")
        self.task("api-server", "codex")
        self.assertEqual(agenthealth.levels(), {("api-server", "claude"): 3, ("api-server", "codex"): 2})
        self.assertEqual(agents.levels(), agenthealth.levels())

    def test_unreadable_log_means_level_one(self):
        with mock.patch.object(agenthealth, "levels", side_effect=OSError("disk gone")):
            self.assertEqual(agents.levels(), {})
            self.assertEqual(attrs(agents.item_row(item(), agents.levels()))["data-level"], "1")


class SharedScript(unittest.TestCase):
    def test_generator_and_motion_rules(self):
        js = app.KIOSK_JS
        for name in ("function critter(", "function critters(", "function crHash(", "function crRandom(", "const crSeed"):
            self.assertEqual(js.count(name), 1, name)
        self.assertIn('shape-rendering="crispEdges"', js)
        self.assertIn('aria-hidden="true"', js)
        self.assertIn("prefers-reduced-motion:reduce", js)
        self.assertIn(".cr-still .cr *", js)
        self.assertIn("#face.quiet .cr *", js)

    def test_page_draws_cards_and_faces(self):
        self.assertIn("critters(el, keep)", app.PAGE)  # keep: crKeep, the creatures the card had
        self.assertIn("%%KIOSK_JS%%\ncritters();", app.PAGE)
        self.assertIn("classList.toggle('cr-still', quietNow)", app.PAGE)
        self.assertEqual(len(re.findall(r"critter\(crSeed\(m\), m\.status, m\.level, m\.agent\)", app.PAGE)), 2)


if __name__ == "__main__":
    unittest.main()
