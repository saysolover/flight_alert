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


class WarmupNoticeTest(unittest.TestCase):
    def test_notice_sent_once_after_warmup(self):
        sent = []
        orig, fa.post = fa.post, lambda w, p: sent.append(p)
        try:
            with tempfile.TemporaryDirectory() as d:
                fa.run(CFG, "t", None, d, rows=[row(150000)])           # first obs, now
                fa.run(CFG, "t", "http://x", d, rows=[row(150000)])     # still warming up
                self.assertEqual(sent, [])
                old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
                p = f"{d}/obs-2000-01.csv"
                with open(p, "w", encoding="utf-8") as f:
                    f.write("ts,kind,o_ap,d_ap,dep_at,ret_at,airline,price\n"
                            f"{old},ow,ICN,NRT,2026-12-01T08:00:00+09:00,,7C,150000\n")
                fa.run(CFG, "t", "http://x", d, rows=[row(150000)])
                fa.run(CFG, "t", "http://x", d, rows=[row(150000)])
                self.assertEqual(len(sent), 1)
        finally:
            fa.post = orig


def frow(kind, o, d, price, dep="2026-12-03", ret="", airline="MM"):
    return {"kind": kind, "o_ap": o, "d_ap": d, "dep_at": dep + "T08:00:00+09:00",
            "ret_at": ret + ("T20:00:00+09:00" if ret else ""), "airline": airline, "price": price,
            "gate": "g", "link": ""}


class SummaryTest(unittest.TestCase):
    ORIGINS = ["ICN", "GMP"]

    def rows(self, n=30):
        r = [frow("ow", "ICN", "NRT", 90000 + i, dep=f"2026-12-{1 + i % 28:02d}") for i in range(n)]
        r += [frow("ow", "GMP", "NRT", 71000, dep="2026-12-05", airline="7C"),   # GMP cheaper than ICN
              frow("ow", "NRT", "ICN", 68000, dep="2026-12-10"), frow("ow", "NRT", "GMP", 99000),
              frow("rt", "ICN", "NRT", 140000, ret="2026-12-08"), frow("rt", "GMP", "NRT", 150000, ret="2026-12-09")]
        return r

    def test_merges_origins_and_picks_cheapest(self):
        a = fa.build_summary(self.rows(), self.ORIGINS, NOW)["airports"]["NRT"]
        self.assertEqual(a["ow_out"], {"price": 71000, "origin": "GMP", "date": "2026-12-05", "airline": "7C"})
        self.assertEqual(a["ow_back"], {"price": 68000, "dest": "ICN", "date": "2026-12-10", "airline": "MM"})
        self.assertEqual(a["rt"], {"price": 140000, "origin": "ICN", "dep": "2026-12-03", "ret": "2026-12-08",
                                   "airline": "MM"})
        self.assertEqual((a["samples"], a["median_ow_out"]), (31, 90014))

    def test_ow_back_skips_imminent_departures(self):
        soon = frow("ow", "NRT", "ICN", 30000, dep="2026-09-26")      # 6 days after NOW: excluded
        edge = frow("ow", "NRT", "ICN", 50000, dep="2026-09-27")      # exactly 7 days: included
        a = fa.build_summary(self.rows() + [soon, edge], self.ORIGINS, NOW)["airports"]["NRT"]
        self.assertEqual(a["ow_back"]["price"], 50000)

    def test_ow_back_omitted_when_only_imminent(self):
        rows = [r for r in self.rows() if not (r["kind"] == "ow" and r["o_ap"] == "NRT")]
        rows.append(frow("ow", "NRT", "ICN", 30000, dep="2026-09-22"))
        a = fa.build_summary(rows, self.ORIGINS, NOW)["airports"]["NRT"]
        self.assertNotIn("ow_back", a)
        self.assertIn("ow_out", a)

    def test_omits_airport_without_rt_or_enough_samples(self):
        no_rt = [r for r in self.rows() if r["kind"] != "rt"]
        self.assertEqual(fa.build_summary(no_rt, self.ORIGINS, NOW)["airports"], {})
        self.assertEqual(fa.build_summary(self.rows(n=10), self.ORIGINS, NOW)["airports"], {})

    def test_run_writes_summary_file(self):
        with tempfile.TemporaryDirectory() as d:
            fa.run(CFG, "t", None, d, rows=self.rows())
            s = fa.read_json(f"{d}/summary.json")
            self.assertIn("NRT", s["airports"])
            self.assertIn("참고가", s["source"])


if __name__ == "__main__":
    unittest.main()
