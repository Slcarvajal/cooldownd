import types
import unittest

import tests  # isolation first
import app
from modules import agents

assert tests.HOME  # the isolation ran: temp HOME, no network, no commands


def mod(name):
    return types.SimpleNamespace(NAME=name)


class FaceSources(unittest.TestCase):
    def test_remote_modules_on_the_hub(self):
        mods = [mod("desk.services"), mod("pi.life"), mod("desk.agents"), mod("desk.agenthealth"), mod("power")]
        self.assertEqual(app.face_sources(mods), ("desk.agenthealth", "pi.life"))

    def test_local_modules(self):
        self.assertEqual(app.face_sources([mod("agenthealth"), mod("life"), mod("coop")]), ("agenthealth", "life"))

    def test_none(self):
        self.assertEqual(app.face_sources([mod("overview"), mod("lifeline"), mod("desk.healthagent")]), ("", ""))


class Options(unittest.TestCase):
    def test_claude_code_menu(self):
        lines = ["Do you want to proceed?", "❯ 1. Yes", "  2. Yes, and don't ask again for this command",
                 "  3. No, and tell Claude what to do differently (esc)"]
        self.assertEqual(agents.options(lines), {"1": "Yes", "2": "Yes, and don't ask…", "3": "No, and tell Claude…"})

    def test_plain_list_cut_at_the_colon(self):
        lines = ["Two options:", "1. Redis: already in docker-compose", "2. In memory - no new dependency"]
        self.assertEqual(agents.options(lines), {"1": "Redis", "2": "In memory"})

    def test_last_one_wins_and_only_recent_lines(self):
        old = ["1. Old choice"] + ["text"] * 20
        self.assertEqual(agents.options(old), {})
        self.assertEqual(agents.options(["1. First", "more", "1. Second"]), {"1": "Second"})

    def test_no_options(self):
        self.assertEqual(agents.options(["Want me to push?", "4. not a choice", "10. nor this"]), {})


if __name__ == "__main__":
    unittest.main()
