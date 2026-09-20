import tempfile, tomllib, unittest
from datetime import datetime, timedelta, timezone

import flight_alert as fa

with open("config.toml", "rb") as f:
    CFG = tomllib.load(f)
NOW = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)


def row(price, dep="2026-11-10", ret="", kind="ow", ts=None, airline="7C"):
    r = {"kind": kind, "o_ap": "ICN", "d_ap": "NRT", "dep_at": dep + "T08:00:00+09:00",
         "ret_at": ret, "airline": airline, "price": price, "gate": "g", "link": ""}
    r["ts"] = (ts or NOW - timedelta(days=5)).isoformat(timespec="seconds")
    return r


def history(n=30, base=150000):
    # varied prices, different departure dates so none is a "same date" comparison
    return [row(base + (i % 10) * 3000, dep=f"2026-11-{1 + i % 28:02d}",
                ts=NOW - timedelta(days=1 + i)) for i in range(n)]


class JudgeTest(unittest.TestCase):
    def test_alerts_on_big_drop(self):
        j = fa.judge(row(100000), history(), CFG, NOW)
        self.assertIsNotNone(j)
        self.assertGreater(j["drop"], 0.15)

    def test_no_alert_on_small_drop(self):
        self.assertIsNone(fa.judge(row(140000), history(), CFG, NOW))

    def test_no_alert_when_too_few_samples(self):
        self.assertIsNone(fa.judge(row(100000), history(n=10), CFG, NOW))

    def test_no_alert_if_not_new_low_for_that_date(self):
        h = history() + [row(90000, dep="2026-11-10", ts=NOW - timedelta(days=2))]
        self.assertIsNone(fa.judge(row(100000), h, CFG, NOW))

    def test_other_series_ignored(self):
        h = [dict(r, d_ap="KIX") for r in history()]
        self.assertIsNone(fa.judge(row(100000), h, CFG, NOW))

    def test_max_price_filter(self):
        cfg = {**CFG, "alert": {**CFG["alert"], "max_price": 90000}}
        self.assertIsNone(fa.judge(row(100000), history(), cfg, NOW))


class StoreTest(unittest.TestCase):
    def test_only_changes_are_stored_and_no_realert(self):
        with tempfile.TemporaryDirectory() as d:
            future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
            rows = [row(120000, dep=future)]
            fa.run(CFG, "t", None, d, rows=rows)
            fa.run(CFG, "t", None, d, rows=rows)          # unchanged -> nothing stored
            self.assertEqual(len(fa.load_history(d)), 1)
            fa.run(CFG, "t", None, d, rows=[row(110000, dep=future)])
            self.assertEqual(len(fa.load_history(d)), 2)


class WarmupTest(unittest.TestCase):
    def test_no_alert_during_warmup(self):
        with tempfile.TemporaryDirectory() as d:
            future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
            fa.run(CFG, "t", None, d, rows=[row(150000 + i, dep=future, airline=f"A{i}") for i in range(30)])
            self.assertEqual(fa.run(CFG, "t", None, d, rows=[row(50000, dep=future, airline="A0")]), [])


if __name__ == "__main__":
    unittest.main()
