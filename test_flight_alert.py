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

    CLASSES = {"fsc": ["KE", "OZ", "JL", "NH"], "hsc": ["YP", "WE"], "lcc": ["7C", "MM"]}

    def test_class_breakdown_keeps_existing_fields(self):
        rows = self.rows() + [frow("ow", "ICN", "NRT", 120000, airline="KE"),
                              frow("rt", "ICN", "NRT", 300000, ret="2026-12-09", airline="KE"),
                              frow("ow", "ICN", "NRT", 10000, airline="H1"),   # unclassified: overall only
                              frow("rt", "ICN", "NRT", 20000, ret="2026-12-09", airline="ET")]
        plain = fa.build_summary(rows, self.ORIGINS, NOW)["airports"]["NRT"]
        a = fa.build_summary(rows, self.ORIGINS, NOW, classes=self.CLASSES)["airports"]["NRT"]
        self.assertEqual(a["ow_out"], plain["ow_out"])                      # overall still counts H1
        self.assertEqual(a["ow_out"]["price"], 10000)
        self.assertEqual(a["rt"]["price"], 20000)
        self.assertEqual(a["ow_out_by_class"]["fsc"]["price"], 120000)
        self.assertEqual(a["ow_out_by_class"]["lcc"]["price"], 71000)
        self.assertEqual(a["rt_by_class"]["fsc"]["price"], 300000)
        self.assertEqual(a["rt_by_class"]["lcc"]["price"], 140000)
        self.assertNotIn("hsc", a["ow_out_by_class"])                       # no HSC fares -> key omitted
        self.assertNotIn("hsc", a["rt_by_class"])
        self.assertNotIn("H1", str(a["ow_out_by_class"]) + str(a["rt_by_class"]))

    def test_no_class_fields_without_config(self):
        a = fa.build_summary(self.rows(), self.ORIGINS, NOW)["airports"]["NRT"]
        self.assertNotIn("rt_by_class", a)

    def test_run_uses_config_classes(self):
        with tempfile.TemporaryDirectory() as d:
            fa.run(CFG, "t", None, d, rows=self.rows())
            a = fa.read_json(f"{d}/summary.json")["airports"]["NRT"]
            self.assertIn("lcc", a["ow_out_by_class"])

    def test_sufficient_airport_has_no_sparse_key(self):
        a = fa.build_summary(self.rows(), self.ORIGINS, NOW)["airports"]["NRT"]
        self.assertNotIn("sparse", a)
        self.assertEqual(set(a), {"ow_out", "rt", "median_ow_out", "samples", "ow_back"})

    def test_airport_without_rt_is_kept_as_sparse(self):
        no_rt = [r for r in self.rows() if r["kind"] != "rt"]
        a = fa.build_summary(no_rt, self.ORIGINS, NOW, classes=self.CLASSES)["airports"]["NRT"]
        self.assertTrue(a["sparse"])
        self.assertEqual(a["ow_out"]["price"], 71000)
        self.assertNotIn("rt", a)
        self.assertNotIn("rt_by_class", a)
        self.assertIn("ow_out_by_class", a)

    def test_low_sample_airport_is_kept_as_sparse(self):
        rows = [frow("ow", "ICN", "CTS", 90000 + i, dep=f"2026-12-{1 + i:02d}") for i in range(5)]
        rows.append(frow("rt", "ICN", "CTS", 200000, ret="2026-12-09"))
        a = fa.build_summary(rows, self.ORIGINS, NOW)["airports"]["CTS"]
        self.assertEqual((a["sparse"], a["samples"], a["rt"]["price"]), (True, 5, 200000))

    def test_rt_only_airport_and_back_only_airport(self):
        rows = [frow("rt", "ICN", "SDJ", 250000, ret="2026-12-09"), frow("ow", "KIJ", "ICN", 90000, dep="2026-12-09")]
        s = fa.build_summary(rows, self.ORIGINS, NOW, classes=self.CLASSES)["airports"]
        self.assertEqual(set(s), {"SDJ"})                                      # back-only airport not listed
        self.assertTrue(s["SDJ"]["sparse"])
        self.assertNotIn("ow_out", s["SDJ"])
        self.assertEqual(s["SDJ"]["samples"], 0)

    def test_existing_fields_unchanged_by_extra_sparse_airports(self):
        base = fa.build_summary(self.rows(), self.ORIGINS, NOW, classes=self.CLASSES)["airports"]
        extra = self.rows() + [frow("ow", "ICN", "HIJ", 80000)]
        both = fa.build_summary(extra, self.ORIGINS, NOW, classes=self.CLASSES)["airports"]
        self.assertEqual(both["NRT"], base["NRT"])
        self.assertTrue(both["HIJ"]["sparse"])

    def test_dates_uses_same_airport_set(self):
        rows = self.rows() + [frow("ow", "ICN", "HIJ", 80000, dep="2026-12-04")]
        s = fa.build_summary(rows, self.ORIGINS, NOW, classes=self.CLASSES)
        d = fa.build_dates(rows, self.ORIGINS, NOW, self.CLASSES, set(s["airports"]))
        self.assertEqual(set(d["airports"]), set(s["airports"]))
        self.assertIn("2026-12-04", d["airports"]["HIJ"]["out"])

    def test_run_writes_summary_file(self):
        with tempfile.TemporaryDirectory() as d:
            fa.run(CFG, "t", None, d, rows=self.rows())
            s = fa.read_json(f"{d}/summary.json")
            self.assertIn("NRT", s["airports"])
            self.assertIn("참고가", s["source"])


class DatesTest(unittest.TestCase):
    ORIGINS = ["ICN", "GMP"]
    CLASSES = {"fsc": ["KE", "OZ"], "hsc": ["YP"], "lcc": ["7C", "MM"]}
    AIRPORTS = {"NRT"}

    def build(self, rows):
        return fa.build_dates(rows, self.ORIGINS, NOW, self.CLASSES, self.AIRPORTS)["airports"]["NRT"]

    def test_out_keeps_cheapest_per_date_and_class(self):
        a = self.build([frow("ow", "ICN", "NRT", 90000, airline="MM"), frow("ow", "GMP", "NRT", 80000, airline="7C"),
                        frow("ow", "ICN", "NRT", 170000, airline="KE"), frow("ow", "GMP", "NRT", 160000, airline="OZ")])
        self.assertEqual(a["out"]["2026-12-03"], {"lcc": [80000, "GMP", "7C"], "fsc": [160000, "GMP", "OZ"]})

    def test_unclassified_excluded(self):
        a = self.build([frow("ow", "ICN", "NRT", 10000, airline="H1"), frow("ow", "ICN", "NRT", 20000, airline="ET"),
                        frow("rt", "ICN", "NRT", 30000, ret="2026-12-08", airline="H1")])
        self.assertEqual((a["out"], a["rt"]), ({}, {}))

    def test_past_departures_excluded(self):
        a = self.build([frow("ow", "ICN", "NRT", 50000, dep="2026-09-19"),
                        frow("ow", "ICN", "NRT", 60000, dep="2026-09-20")])          # today is included
        self.assertEqual(list(a["out"]), ["2026-09-20"])

    def test_rt_key_format(self):
        a = self.build([frow("rt", "ICN", "NRT", 222198, ret="2026-12-08")])
        self.assertEqual(a["rt"], {"2026-12-03|2026-12-08": {"lcc": [222198, "ICN", "MM"]}})

    def test_back_direction(self):
        a = self.build([frow("ow", "NRT", "ICN", 104923, dep="2026-12-08", airline="MM"),
                        frow("ow", "NRT", "GMP", 99000, dep="2026-12-08", airline="7C")])
        self.assertEqual(a["back"], {"2026-12-08": {"lcc": [99000, "GMP", "7C"]}})
        self.assertEqual(a["out"], {})

    def test_non_target_airport_excluded(self):
        d = fa.build_dates([frow("ow", "ICN", "KIX", 70000)], self.ORIGINS, NOW, self.CLASSES, self.AIRPORTS)
        self.assertEqual(list(d["airports"]), ["NRT"])
        self.assertEqual(d["airports"]["NRT"]["out"], {})

    def test_run_writes_compact_dates_file(self):
        with tempfile.TemporaryDirectory() as d:
            fa.run(CFG, "t", None, d, rows=SummaryTest().rows())
            with open(f"{d}/dates.json", encoding="utf-8") as f:
                raw = f.read()
            self.assertNotIn(" ", raw)
            self.assertIn("2026-12-03", fa.read_json(f"{d}/dates.json")["airports"]["NRT"]["out"])


if __name__ == "__main__":
    unittest.main()
