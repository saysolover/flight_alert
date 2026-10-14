import csv, json, subprocess, sys, tempfile, tomllib, unittest
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


class QuotaTest(unittest.TestCase):
    TODAY = NOW.date()

    def cand(self, days, drop):
        dep = (self.TODAY + timedelta(days=days)).isoformat()
        return (frow("ow", "ICN", "NRT", 1, dep=dep), {"drop": drop}, {}, f"{days}-{drop}")

    def split(self, sel):
        n = [0, 0, 0]
        for r, *_ in sel:
            d = (datetime.fromisoformat(r["dep_at"][:10]).date() - self.TODAY).days
            n[fa.bucket(d, CFG["alert"]["buckets"])] += 1
        return n

    def test_exact_quota_when_all_buckets_overflow(self):
        c = [self.cand(5, 0.9 - i / 100) for i in range(8)] + [self.cand(60, 0.5 - i / 100) for i in range(8)]             + [self.cand(120, 0.3 - i / 100) for i in range(8)]
        self.assertEqual(self.split(fa.select_alerts(c, CFG, self.TODAY)), [3, 4, 3])

    def test_unused_slots_spill_over_keeping_total(self):
        c = [self.cand(5, 0.9 - i / 100) for i in range(8)] + [self.cand(60, 0.5 - i / 100) for i in range(8)]             + [self.cand(120, 0.3), self.cand(120, 0.29)]
        sel = fa.select_alerts(c, CFG, self.TODAY)
        self.assertEqual(len(sel), 10)
        self.assertEqual(self.split(sel)[2], 2)

    def test_fewer_than_max_sends_all(self):
        c = [self.cand(5, 0.9), self.cand(5, 0.8), self.cand(5, 0.7), self.cand(5, 0.6), self.cand(60, 0.4)]
        self.assertEqual(len(fa.select_alerts(c, CFG, self.TODAY)), 5)

    def test_config_validation(self):
        fa.check_config(CFG)
        bad_len = {"alert": {**CFG["alert"], "bucket_quota": [5, 5]}}
        bad_sum = {"alert": {**CFG["alert"], "bucket_quota": [3, 4, 4]}}
        for cfg in (bad_len, bad_sum):
            with self.assertRaises(ValueError):
                fa.check_config(cfg)


class AlternatingSpillTest(unittest.TestCase):
    TODAY = NOW.date()

    def cand(self, days, drop):
        return QuotaTest.cand(self, days, drop)

    def test_spill_goes_to_far_buckets_first_one_at_a_time(self):
        c = [self.cand(5, 0.9 - i / 100) for i in range(6)] + [self.cand(60, 0.5 - i / 100) for i in range(8)]
        sel = fa.select_alerts(c, CFG, self.TODAY)
        n = QuotaTest.split(self, sel)
        self.assertEqual(n, [4, 6, 0])          # 3 free slots: 31-90, <=30, 31-90
        self.assertEqual(len(sel), 10)


def write_obs(d, rows):
    with open(f"{d}/obs-2026-09.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fa.FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["ts"]))


EARLY = NOW.replace(hour=0, minute=10)       # 10 min after the 00:00Z slot: inside the grace period


class CaptureMixin:
    def setUp(self):
        self.posts = []
        self.orig = fa.post
        fa.post = lambda w, p: self.posts.append(p) if "embeds" in p else None   # skip the one-time warm-up notice

    def tearDown(self):
        fa.post = self.orig


class DigestFlowTest(CaptureMixin, unittest.TestCase):
    def setup_dir(self, d):
        write_obs(d, history())
        return d

    def test_collect_only_queues_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            self.setup_dir(d)
            sent = fa.run(CFG, "t", "http://x", d, rows=[row(100000)], now=EARLY)
            self.assertEqual((sent, self.posts), ([], []))
            self.assertEqual(len(fa.read_json(f"{d}/pending.json")), 1)

    def test_missed_digest_is_sent_once_after_grace(self):
        with tempfile.TemporaryDirectory() as d:
            self.setup_dir(d)
            fa.run(CFG, "t", "http://x", d, rows=[row(100000)], now=EARLY)
            late = NOW.replace(hour=0, minute=40)                      # 40 min late
            sent = fa.run(CFG, "t", "http://x", d, rows=[row(100000)], now=late)
            self.assertEqual(len(sent), 1)
            self.assertEqual(len(self.posts), 1)
            fa.run(CFG, "t", "http://x", d, rows=[row(100000)], now=late + timedelta(minutes=20))
            self.assertEqual(len(self.posts), 1)                        # same slot: no duplicate
            self.assertEqual(fa.read_json(f"{d}/pending.json"), {})

    def test_digest_mode_sends_for_slot_and_not_twice(self):
        with tempfile.TemporaryDirectory() as d:
            self.setup_dir(d)
            fa.run(CFG, "t", "http://x", d, rows=[row(100000)], now=EARLY)
            at = NOW.replace(hour=0, minute=0, second=5)
            self.assertEqual(len(fa.run_digest(CFG, "http://x", d, now=at)), 1)
            self.assertEqual(fa.run_digest(CFG, "http://x", d, now=at + timedelta(minutes=1)), [])
            self.assertEqual(len(self.posts), 1)

    def test_early_dispatch_maps_to_its_own_slot(self):
        s = fa.slot_for(NOW.replace(hour=8, minute=55), [0, 9])
        self.assertEqual((s.day, s.hour), (NOW.day, 9))

    def test_failed_send_keeps_candidates_for_retry(self):
        with tempfile.TemporaryDirectory() as d:
            self.setup_dir(d)
            fa.run(CFG, "t", "http://x", d, rows=[row(100000)], now=EARLY)

            def boom(w, p):
                raise OSError("down")
            fa.post = boom
            self.assertEqual(fa.run_digest(CFG, "http://x", d, now=NOW.replace(hour=0, minute=1)), [])
            self.assertEqual(len(fa.read_json(f"{d}/pending.json")), 1)
            self.assertNotIn("last_digest_slot", fa.read_json(f"{d}/state.json"))

    def test_pending_expires_after_48h_and_drops_when_price_rises(self):
        with tempfile.TemporaryDirectory() as d:
            self.setup_dir(d)
            fa.run(CFG, "t", "http://x", d, rows=[row(100000)], now=EARLY)
            fa.run(CFG, "t", "http://x", d, rows=[row(149000)], now=EARLY + timedelta(minutes=5))
            self.assertEqual(fa.read_json(f"{d}/pending.json"), {})     # price rose past the limit
        with tempfile.TemporaryDirectory() as d:
            self.setup_dir(d)
            fa.run(CFG, "t", "http://x", d, rows=[row(100000)], now=EARLY)
            late = EARLY + timedelta(hours=49)
            self.assertEqual(fa.run_digest(CFG, "http://x", d, now=late.replace(hour=9, minute=0)), [])

    def test_mode_dispatch_rejects_unknown_mode(self):
        r = subprocess.run([sys.executable, "flight_alert.py", "--mode", "bogus"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown mode", r.stderr)


class DigestGroupingTest(CaptureMixin, unittest.TestCase):
    def pend(self, items):
        out = {}
        for i, (o, d, dep, price, airline) in enumerate(items):
            r = dict(row(price, dep=dep, airline=airline), o_ap=o, d_ap=d)
            ak = f"ow|{o}|{d}|{dep}|"
            out[ak] = {"r": r, "j": {"median": 150000, "samples": 40, "drop": 1 - price / 150000, "limit": 10**9},
                       "extra": {}, "fkey": ak, "found_at": "2026-09-20T00:00:00+00:00"}
        return out

    def digest(self, items):
        with tempfile.TemporaryDirectory() as d:
            for n, o in (("pending", self.pend(items)), ("alerted", {}), ("state", {})):
                fa.write_json(f"{d}/{n}.json", o)
            sent = fa.run_digest(CFG, "http://x", d, now=NOW.replace(hour=0, minute=1))
            return sent, fa.read_json(f"{d}/pending.json"), fa.read_json(f"{d}/alerted.json")

    def test_same_route_same_bucket_is_one_alert_with_other_dates(self):
        sent, pending, alerted = self.digest([("ICN", "NRT", f"2026-09-{25 + i}", 90000 + i * 3000, "7C") for i in range(4)])
        self.assertEqual(len(sent), 1)
        e = self.posts[0]["embeds"][0]
        self.assertIn("90,000원", e["description"])
        self.assertIn("다른 날짜: 09/26 93,000 · 09/27 96,000 · 09/28 99,000", e["description"])
        self.assertEqual((pending, len(alerted)), ({}, 4))             # all members recorded and cleared

    def test_other_dates_capped_at_five_with_remainder(self):
        sent, _, _ = self.digest([("ICN", "NRT", f"2026-09-{22 + i}", 90000 + i * 1000, "7C") for i in range(8)])
        desc = self.posts[0]["embeds"][0]["description"]
        self.assertEqual(desc.count(" · "), 4)
        self.assertTrue(desc.split("\n")[-2].endswith("외 2"))

    def test_freed_slots_go_to_other_routes(self):
        items = [("ICN", "NRT", f"2026-09-{22 + i}", 80000 + i * 1000, "7C") for i in range(6)]   # one route, <=30d
        items += [("ICN", d, "2026-09-25", 85000, "7C") for d in ("FUK", "KIX", "OKA", "NGO", "CTS", "HND")]
        sent, pending, _ = self.digest(items)
        self.assertEqual(len(sent), 7)                                  # 1 grouped NRT + 6 other routes
        self.assertEqual(pending, {})

    def test_same_route_in_different_buckets_is_sent_separately(self):
        sent, _, _ = self.digest([("ICN", "NRT", "2026-09-25", 90000, "7C"), ("ICN", "NRT", "2026-11-25", 91000, "7C"),
                                  ("ICN", "NRT", "2027-01-25", 92000, "7C")])
        self.assertEqual(len(sent), 3)

    def test_unselected_group_members_stay_pending(self):
        items = [("ICN", d, "2026-09-25", 70000 + i, "7C") for i, d in enumerate("ABCDEFGHIJKL")]   # 12 routes, all <=30d
        sent, pending, alerted = self.digest(items)
        self.assertEqual(len(sent), 10)
        self.assertEqual((len(pending), len(alerted)), (2, 10))


def deal_hist(n=60, base=150000, offset=45):
    # dep 45 days after each observation -> 31-90 day bucket; varied prices, median ~152000
    out = []
    for i in range(n):
        ts = NOW - timedelta(days=1 + i % 50)
        dep = (ts.date() + timedelta(days=offset)).isoformat()
        out.append(row(base + (i % 5) * 1000, dep=dep, ts=ts))
    return out


DEAL_DEP = (NOW.date() + timedelta(days=45)).isoformat()


class DealConditionTest(unittest.TestCase):
    def deals(self, price=100000, hist=None, dep=DEAL_DEP):
        return fa.find_deals([row(price, dep=dep)], hist if hist is not None else deal_hist(), CFG, NOW)

    def test_passes_when_all_conditions_hold(self):
        d = self.deals()
        self.assertEqual(len(d), 1)
        self.assertGreater(d[0]["drop"], 0.30)

    def test_needs_15_days_left(self):
        near = (NOW.date() + timedelta(days=14)).isoformat()
        self.assertEqual(self.deals(dep=near, hist=deal_hist(offset=14)), [])
        ok = (NOW.date() + timedelta(days=15)).isoformat()
        self.assertEqual(len(self.deals(dep=ok, hist=deal_hist(offset=15))), 1)

    def test_needs_50_samples(self):
        self.assertEqual(self.deals(hist=deal_hist(n=49)), [])
        self.assertEqual(len(self.deals(hist=deal_hist(n=50))), 1)

    def test_must_be_series_wide_low_not_just_that_date(self):
        other_date_low = row(95000, dep=(NOW.date() + timedelta(days=70)).isoformat(), ts=NOW - timedelta(days=2))
        self.assertEqual(self.deals(hist=deal_hist() + [other_date_low]), [])

    def test_other_series_low_does_not_block(self):
        other = dict(row(50000, ts=NOW - timedelta(days=2)), d_ap="KIX")
        self.assertEqual(len(self.deals(hist=deal_hist() + [other])), 1)

    def test_needs_30_percent_below_median(self):
        self.assertEqual(self.deals(price=110000), [])          # ~28% below
        self.assertEqual(len(self.deals(price=105000)), 1)      # ~31% below


class DealSendTest(CaptureMixin, unittest.TestCase):
    def deal(self, dep, price=100000, o="ICN", d="NRT", airline="7C"):
        r = dict(row(price, dep=dep, airline=airline), o_ap=o, d_ap=d)
        return {"r": r, "drop": 0.34, "median": 150000, "samples": 60}

    def send(self, deals, state=None, alerted=None, pending=None, now=NOW):
        state = {} if state is None else state
        return fa.send_deals(CFG, "http://x", deals, state, {} if alerted is None else alerted,
                             {} if pending is None else pending, now), state

    def test_same_series_dates_are_one_alert_and_one_cap_slot(self):
        done, state = self.send([self.deal("2026-11-10"), self.deal("2026-11-11"), self.deal("2026-11-12")])
        self.assertEqual(len(self.posts), 1)
        desc = self.posts[0]["embeds"][0]["description"]
        self.assertIn("출발 2026-11-10", desc)
        self.assertIn("다른 날짜: 11/11 100,000 · 11/12 100,000", desc)
        self.assertEqual(state["deals_day"]["n"], 1)
        self.assertEqual(len(done), 3)

    def test_deal_embed_is_visually_distinct_and_marked_reference_price(self):
        self.send([self.deal("2026-11-10")])
        e = self.posts[0]["embeds"][0]
        self.assertTrue(e["title"].startswith("🔥 특가"))
        self.assertIn("color", e)
        self.assertIn("참고가", e["description"])

    def test_daily_cap_of_three_and_resets_next_kst_day(self):
        deals = [self.deal("2026-11-10", d=a) for a in ("NRT", "HND", "KIX", "FUK", "OKA")]
        _, state = self.send(deals)
        self.assertEqual(len(self.posts), 3)
        self.send(deals, state=state, now=NOW + timedelta(hours=1))
        self.assertEqual(len(self.posts), 3)                     # still capped (same KST day)
        self.send([self.deal("2026-11-20", d="NGO")], state=state, now=NOW + timedelta(days=1))
        self.assertEqual(len(self.posts), 4)                     # new KST day

    def test_same_route_date_not_repeated_within_72h(self):
        _, state = self.send([self.deal("2026-11-10")])
        self.send([self.deal("2026-11-10", price=90000)], state=state, now=NOW + timedelta(hours=71))
        self.assertEqual(len(self.posts), 1)
        self.send([self.deal("2026-11-10", price=90000)], state=state, now=NOW + timedelta(hours=73))
        self.assertEqual(len(self.posts), 2)

    def test_sent_deal_leaves_pending_and_is_marked_alerted(self):
        r = self.deal("2026-11-10")["r"]
        ak = "|".join((r["kind"], r["o_ap"], r["d_ap"], "2026-11-10", ""))
        pending, alerted = {ak: {"r": r}}, {}
        self.send([self.deal("2026-11-10")], alerted=alerted, pending=pending)
        self.assertEqual((pending, alerted[ak]), ({}, 100000))

    def test_failed_deal_send_records_nothing(self):
        def boom(w, p):
            raise OSError("down")
        fa.post = boom
        done, state = self.send([self.deal("2026-11-10")])
        self.assertEqual((done, state.get("deals_sent"), state["deals_day"]["n"]), (set(), {}, 0))


class DealEmbedTest(unittest.TestCase):
    def x(self, dep, price, link="", ret="", drop=0.55):
        r = dict(row(price, dep=dep, ret=ret + ("T19:00:00+09:00" if ret else ""), kind="rt" if ret else "ow"),
                 o_ap="ICN", d_ap="KMJ", link=link)
        return {"r": r, "drop": drop, "median": 260000, "samples": 76}

    def embed(self, *xs):
        return fa.deal_embed({"rows": list(xs), "drop": 0.55})

    def test_same_layout_as_digest_embed_with_deal_marks(self):
        x = self.x("2026-11-01", 116359, "/s/a")
        e = self.embed(x)
        d = fa.alert_embed(x["r"], x, {})
        self.assertEqual(e["description"], d["description"])
        self.assertEqual(e["title"], "🔥 특가 · " + d["title"])
        self.assertEqual(e["color"], 0xE67E22)
        self.assertEqual(e["url"], "https://www.aviasales.com/s/a")
        self.assertIn("중앙값 260,000원 대비 -55%", e["description"])

    def test_no_link_means_no_url_and_no_line_links(self):
        e = self.embed(self.x("2026-11-01", 116359))
        self.assertNotIn("url", e)
        self.assertNotIn("[예약]", e["description"])

    def test_representative_is_cheapest_and_others_listed_ascending(self):
        e = self.embed(self.x("2026-11-02", 120000, "/s/b"), self.x("2026-11-01", 116359, "/s/a"),
                       self.x("2026-11-03", 118000, "/s/c"))
        self.assertEqual(e["url"], "https://www.aviasales.com/s/a")
        self.assertIn("출발 2026-11-01", e["description"])
        self.assertIn("다른 날짜: 11/03 118,000 · 11/02 120,000", e["description"])
        self.assertNotIn("[예약]", e["description"])

    def test_others_capped_at_five(self):
        e = self.embed(*[self.x(f"2026-11-{1 + i:02d}", 100000 + i * 100) for i in range(8)])
        self.assertTrue(e["description"].split("\n")[-2].endswith("외 2"))

    def test_round_trip_shows_nights_and_return(self):
        e = self.embed(self.x("2026-11-01", 200000, ret="2026-11-06"))
        self.assertIn("왕복 5박", e["title"])
        self.assertIn("귀국 2026-11-06", e["description"])


if __name__ == "__main__":
    unittest.main()
