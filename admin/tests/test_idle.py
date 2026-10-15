import unittest
from unittest import mock

from modules import idle


class Parse(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(idle.parse("(uint64 12345,)"), 12.345)
        self.assertEqual(idle.parse("(uint64 0,)\n"), 0.0)

    def test_garbage(self):
        for out in ("Error: GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown", "(uint64 -5,)",
                    "(int32 12,)", "(uint64 12345,) extra", "uint64 12345"):
            self.assertIsNone(idle.parse(out), out)

    def test_empty(self):
        self.assertIsNone(idle.parse(""))
        self.assertIsNone(idle.parse(None))


class IdleSeconds(unittest.TestCase):
    def setUp(self):
        idle._cache.clear()

    def test_failure_is_none(self):
        # tests/__init__ blocks subprocess.run, so common.run returns "".
        self.assertIsNone(idle.idle_seconds())

    def test_asks_gdbus_and_caches(self):
        with mock.patch.object(idle, "run", return_value="(uint64 5000,)") as run:
            self.assertEqual(idle.idle_seconds(), 5.0)
            self.assertEqual(idle.idle_seconds(), 5.0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[:2], ("gdbus", "call"))
        self.assertIn("org.gnome.Mutter.IdleMonitor.GetIdletime", run.call_args.args)
        self.assertEqual(run.call_args.kwargs, {"timeout": 2})

    def test_cache_expires(self):
        with mock.patch.object(idle, "run", side_effect=["(uint64 1000,)", "(uint64 9000,)"]) as run, \
                mock.patch.object(idle.time, "monotonic", side_effect=[100.0, 103.0, 103.0]):
            self.assertEqual(idle.idle_seconds(), 1.0)
            self.assertEqual(idle.idle_seconds(), 9.0)
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
