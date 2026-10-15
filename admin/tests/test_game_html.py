"""life/game.html: a plain-text check that the page keeps its placeholder and never puts state data
into HTML without esc() (or num() / pct() for numbers)."""
import os
import re
import unittest

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "life", "game.html")
# critter() (app.KIOSK_JS) builds its SVG from numbers and fixed colours only, never from its arguments' text.
SAFE_CALL = re.compile(r"(esc|num|pct|critter)\(")
# A ternary whose branches are both string literals: `${q.done ? ' done' : ''}`
LITERAL_TERNARY = re.compile(r"^[\w.\[\]!= ]+\?\s*'[^'$`]*'\s*:\s*'[^'$`]*'$")
# What an innerHTML assignment may start with: a template, a mapped list, or fill()'s already built html.
# S.avatar is the SVG the panel draws itself (life.avatar), not text from anyone.
INNER_RHS = re.compile(r"^(''[;)]|`|arr\(|rows\.map\(|list\.map\(|html \|\| ''|!b \?|S\.avatar\b)")


def _interpolations(src):
    """Each ${...} expression, nested ones included, with the line it starts on."""
    out = []
    for m in re.finditer(r"\$\{", src):
        depth, i = 1, m.end()
        while depth and i < len(src):
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        out.append((src.count("\n", 0, m.start()) + 1, src[m.end():i - 1].strip()))
    return out


class GameHtml(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PATH, encoding="utf-8") as f:
            cls.src = f.read()
        cls.js = cls.src[cls.src.index("<script>"):cls.src.index("</script>")]

    def test_placeholder(self):
        self.assertEqual(self.src.count("%%KIOSK_JS%%"), 1)
        self.assertEqual(re.findall(r"%%\w+%%", self.src), ["%%KIOSK_JS%%"])

    def test_interpolations_are_escaped(self):
        lines = self.js.split("\n")
        bad = []
        for n, expr in _interpolations(self.js):
            if ".textContent = `" in lines[n - 1]:
                continue  # textContent never parses markup
            if SAFE_CALL.match(expr) or LITERAL_TERNARY.match(expr) or ".map(" in expr:
                continue  # a .map( holds templates whose own ${} are checked one by one
            bad.append((n, expr))
        self.assertEqual(bad, [])

    def test_inner_html_sources(self):
        rhs = re.findall(r"\.innerHTML = (.+)", self.js)
        self.assertTrue(rhs)
        self.assertEqual([r for r in rhs if not INNER_RHS.match(r)], [])

    def test_hooks(self):
        self.assertIn("typeof rumble === 'function' && rumble(400)", self.js)
        self.assertIn("fresh && !d.quiet", self.js)
        self.assertIn("'/action/' + MOD + '/equip/'", self.js)
        for key in ("bars", "mood", "sleep", "boss", "achievements", "titles", "equipped", "cosmetics", "daily",
                    "party", "coop"):
            self.assertIn(f"S.{key}", self.js)


if __name__ == "__main__":
    unittest.main()
