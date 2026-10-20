"""Collect Travelpayouts cached fares, store price changes, alert Discord on significant lows."""
import csv, glob, json, os, statistics, sys, time, tomllib, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
BOOK = "https://www.aviasales.com"
FIELDS = ["ts", "kind", "o_ap", "d_ap", "dep_at", "ret_at", "airline", "price"]


def api_get(token, **params):
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(params), headers={"X-Access-Token": token})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)["data"]
        except urllib.error.HTTPError as e:
            if e.code == 400:  # unknown/unflightable location
                return []
            time.sleep(2 * (attempt + 1))
        except OSError:
            time.sleep(2 * (attempt + 1))
    return []


def fetch_all(token, cfg):
    """Yield normalized rows: one-way out/in and round-trip for every origin x city."""
    s = cfg["search"]
    limit = date.today() + timedelta(days=s["horizon_days"])
    for o in s["origins"]:
        for c in s["cities"]:
            for kind, org, dst, one_way in (("ow", o, c, "true"), ("ow", c, o, "true"), ("rt", o, c, "false")):
                data = api_get(token, origin=org, destination=dst, currency=s["currency"], direct="true",
                               one_way=one_way, unique="false", sorting="price", limit=1000)
                time.sleep(0.1)
                for r in data:
                    if r["airline"] in s["airlines_exclude"] or r["transfers"] or r.get("return_transfers"):
                        continue
                    if date.fromisoformat(r["departure_at"][:10]) > limit:
                        continue
                    yield {"kind": kind, "o_ap": r["origin_airport"], "d_ap": r["destination_airport"],
                           "dep_at": r["departure_at"], "ret_at": r.get("return_at") or "",
                           "airline": r["airline"], "price": r["price"], "gate": r.get("gate", ""),
                           "link": r.get("link", "")}


def read_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj, **kw):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, **kw)


def key(r):
    return "|".join((r["kind"], r["o_ap"], r["d_ap"], r["dep_at"], r["ret_at"], r["airline"]))


def load_history(data_dir):
    rows = []
    for f in sorted(glob.glob(os.path.join(data_dir, "obs-*.csv"))):
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                r["price"] = int(r["price"])
                rows.append(r)
    return rows


def bucket(days, edges):
    for i, e in enumerate(edges):
        if days <= e:
            return i
    return len(edges)


def days_to_dep(r, obs_day):
    return (date.fromisoformat(r["dep_at"][:10]) - obs_day).days


def percentile(sorted_vals, pct):
    return sorted_vals[max(0, int(len(sorted_vals) * pct / 100) - 1)]


def judge(row, history, cfg, now):
    """Return alert info dict, or None. history excludes the current run's rows."""
    a = cfg["alert"]
    if a["max_price"] and row["price"] > a["max_price"]:
        return None
    edges = a["buckets"]
    b = bucket(days_to_dep(row, now.date()), edges)
    since = (now - timedelta(days=a["lookback_days"])).isoformat(timespec="seconds")
    series = [h for h in history if h["kind"] == row["kind"] and h["o_ap"] == row["o_ap"]
              and h["d_ap"] == row["d_ap"] and h["ts"] >= since
              and bucket(days_to_dep(h, date.fromisoformat(h["ts"][:10])), edges) == b]
    if len(series) < a["min_samples"]:
        return None
    prices = sorted(h["price"] for h in series)
    median = prices[len(prices) // 2]
    limit = min(percentile(prices, a["percentile"]), median * (1 - a["min_drop"]))
    if row["price"] > limit:
        return None
    same = [h["price"] for h in series if h["dep_at"][:10] == row["dep_at"][:10] and h["ret_at"][:10] == row["ret_at"][:10]]
    if same and row["price"] >= min(same):
        return None  # not a new low for this departure
    return {"median": median, "samples": len(prices), "drop": 1 - row["price"] / median, "limit": limit}


def check_config(cfg):
    a = cfg["alert"]
    if len(a["bucket_quota"]) != len(a["buckets"]) + 1:
        raise ValueError("bucket_quota must have len(buckets)+1 entries")
    if sum(a["bucket_quota"]) != a["max_alerts_per_run"]:
        raise ValueError("bucket_quota must sum to max_alerts_per_run")


def select_alerts(alerts, cfg, today):
    """Pick up to max_alerts_per_run: per-bucket quota by drop; unused slots go to the other buckets
    one at a time, farthest bucket first, best drop first within a bucket."""
    a = cfg["alert"]
    per = [[] for _ in a["bucket_quota"]]
    for x in sorted(alerts, key=lambda x: -x[1]["drop"]):
        per[bucket(days_to_dep(x[0], today), a["buckets"])].append(x)
    chosen = [x for q, c in zip(a["bucket_quota"], per) for x in c[:q]]
    left = [list(c[q:]) for q, c in zip(a["bucket_quota"], per)]
    slots = a["max_alerts_per_run"] - len(chosen)
    while slots > 0 and any(left):
        for c in reversed(left):
            if slots > 0 and c:
                chosen.append(c.pop(0))
                slots -= 1
    return sorted(chosen, key=lambda x: -x[1]["drop"])


def post(webhook, payload):
    req = urllib.request.Request(webhook, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "flight-alert"})
    urllib.request.urlopen(req, timeout=30).close()


def alert_embed(r, j, extra):
    kind = f"왕복 {extra['nights']}박" if r["kind"] == "rt" else "편도"
    desc = (f"**{r['price']:,}원** ({r['airline']}, {r['gate']})\n"
            f"최근 {j['samples']}건 중앙값 {j['median']:,}원 대비 -{j['drop']:.0%}\n"
            f"출발 {r['dep_at'][:16]}" + (f" / 귀국 {r['ret_at'][:16]}" if r["ret_at"] else ""))
    if extra.get("ow_sum"):
        desc += f"\n편도 합산 {extra['ow_sum']:,}원"
    if extra.get("others"):
        shown = extra["others"][:5]
        more = len(extra["others"]) - len(shown)
        desc += "\n다른 날짜: " + " · ".join(f"{d[5:7]}/{d[8:10]} {p:,}" for d, p in shown) + (f" 외 {more}" if more else "")
    if extra.get("age") is not None:
        desc += "\n" + ("오늘 발견" if extra["age"] == 0 else "어제 발견" if extra["age"] == 1 else f"{extra['age']}일 전 가격")
    desc += "\n_캐시 기반 참고가입니다. 예약 전 재확인하세요._"
    embed = {"title": f"{r['o_ap']} → {r['d_ap']} · {kind}", "description": desc}
    if r["link"]:
        embed["url"] = BOOK + r["link"].split("?")[0] + "?currency=krw"  # route+date search, not the (possibly sold-out) cached offer
    return embed


def notify(webhook, alerts):
    embeds = [alert_embed(*x) for x in alerts]
    for i in range(0, len(embeds), 10):
        post(webhook, {"embeds": embeds[i:i + 10]})


def warmup_notice(webhook, history, min_samples):
    series = {}
    for h in history:
        k = (h["kind"], h["o_ap"], h["d_ap"])
        series[k] = series.get(k, 0) + 1
    ready = sum(1 for n in series.values() if n >= min_samples)
    post(webhook, {"content": f"학습 기간이 끝났습니다. 지금부터 유의하게 낮은 가격이 나오면 알림을 보냅니다.\n"
                              f"누적 관측 {len(history):,}건, 노선 시리즈 {len(series)}개 중 {ready}개가 표본 {min_samples}건 이상입니다."})


KST = timezone(timedelta(hours=9))


def slot_for(now, slots):
    """Latest digest slot (UTC hours) not later than now + 15 min, so an early dispatch maps to its own slot."""
    t = now + timedelta(minutes=15)
    cs = [t.replace(hour=h, minute=0, second=0, microsecond=0) for h in slots]
    cs += [c - timedelta(days=1) for c in cs]
    return max(c for c in cs if c <= t)


def due_digest(state, now, a, force=False):
    """Slot to send now, or None. Not due if already sent for the slot, or (unless forced) within the grace period."""
    slot = slot_for(now, a["digest_slots_utc"])
    if state.get("last_digest_slot", "") >= slot.isoformat():
        return None
    if not force and now - slot < timedelta(minutes=a["digest_grace_min"]):
        return None
    return slot


def found_age(r, now):
    """Days (KST) since the cached fare was found, from the link's search_date=DDMMYYYY; None if absent/malformed."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(r["link"]).query)
        d = datetime.strptime(q["search_date"][0], "%d%m%Y").date()
    except (KeyError, IndexError, ValueError):
        return None
    return max(0, (now.astimezone(KST).date() - d).days)


def too_old(r, a, now):
    age = found_age(r, now)
    return age is not None and age >= a["max_found_age_days"]


def pending_cands(pending, alerted, a, now):
    cutoff = (now - timedelta(hours=a["pending_hours"])).isoformat(timespec="seconds")
    for k in [k for k, v in pending.items() if v["found_at"] < cutoff or v["r"]["dep_at"][:10] < now.date().isoformat()]:
        del pending[k]
    return [(v["r"], v["j"], v["extra"], k) for k, v in pending.items()
            if v["r"]["price"] <= alerted.get(k, 1 << 60) * (1 - a["realert_drop"]) and not too_old(v["r"], a, now)]


def group_cands(cands, cfg, today):
    """One representative (cheapest) per series+airline+bucket; the rest are listed as other dates.
    Returns (row, judge info, extra, key, member keys)."""
    groups = {}
    for c in cands:
        r = c[0]
        b = bucket(days_to_dep(r, today), cfg["alert"]["buckets"])
        groups.setdefault((r["kind"], r["o_ap"], r["d_ap"], r["airline"], b), []).append(c)
    reps = []
    for g in groups.values():
        g.sort(key=lambda c: c[0]["price"])
        extra = dict(g[0][2])
        if len(g) > 1:
            extra["others"] = [(c[0]["dep_at"][:10], c[0]["price"]) for c in g[1:]]
        reps.append((g[0][0], g[0][1], extra, g[0][3], [c[3] for c in g]))
    return reps


def send_digest(cfg, webhook, pending, alerted, state, now, slot):
    """Send the batch for `slot`; returns the items sent. State is only advanced on success."""
    reps = group_cands(pending_cands(pending, alerted, cfg["alert"], now), cfg, now.date())
    sel = select_alerts(reps, cfg, now.date())
    if webhook is None:
        for r, j, extra, _, members in sel:
            print("DIGEST", r["o_ap"], r["d_ap"], r["dep_at"][:10], r["price"], f"-{j['drop']:.0%}", f"(+{len(members) - 1} dates)")
        return []
    try:
        for x in sel:
            x[2]["age"] = found_age(x[0], now)
        if sel:
            notify(webhook, [x[:3] for x in sel])
    except OSError as e:  # retried by the next collect run
        print("digest failed:", type(e).__name__, e)
        return []
    for x in sel:
        for k in x[4]:
            alerted[k] = pending[k]["r"]["price"]
            del pending[k]
    state["last_digest_slot"] = slot.isoformat()
    return sel


def find_deals(rows, history, cfg, now):
    """D2: >= min_days left, >= min_samples in the 60d bucket series, series-wide all-time low, >= drop below median."""
    d, edges = cfg["deal"], cfg["alert"]["buckets"]
    by, low = {}, {}
    for h in history:
        k = (h["kind"], h["o_ap"], h["d_ap"])
        by.setdefault(k, []).append(h)
        low[k] = min(low.get(k, 1 << 60), h["price"])
    since = (now - timedelta(days=cfg["alert"]["lookback_days"])).isoformat(timespec="seconds")
    out = []
    for r in rows:
        days = days_to_dep(r, now.date())
        k = (r["kind"], r["o_ap"], r["d_ap"])
        if days < d["min_days"] or k not in low or r["price"] >= low[k] or too_old(r, cfg["alert"], now):
            continue
        b = bucket(days, edges)
        ser = [h["price"] for h in by[k] if h["ts"] >= since
               and bucket(days_to_dep(h, date.fromisoformat(h["ts"][:10])), edges) == b]
        if len(ser) < d["min_samples"]:
            continue
        med = statistics.median(ser)
        if r["price"] <= med * (1 - d["drop"]):
            out.append({"r": r, "drop": 1 - r["price"] / med, "median": med, "samples": len(ser)})
    return out


def deal_embed(g, now=None):
    """Same layout as a digest alert (cheapest row as representative, other dates listed), marked as a deal."""
    rs = sorted(g["rows"], key=lambda x: x["r"]["price"])
    r = rs[0]["r"]
    extra = {}
    if r["ret_at"]:
        extra["nights"] = (date.fromisoformat(r["ret_at"][:10]) - date.fromisoformat(r["dep_at"][:10])).days
    if len(rs) > 1:
        extra["others"] = [(x["r"]["dep_at"][:10], x["r"]["price"]) for x in rs[1:]]
    if now:
        extra["age"] = found_age(r, now)
    embed = alert_embed(r, rs[0], extra)
    embed["title"] = "🔥 특가 · " + embed["title"]
    embed["color"] = 0xE67E22
    return embed


def send_deals(cfg, webhook, deals, state, alerted, pending, now):
    """Group by series+airline (one alert per group), enforce 72h repeat and daily cap. Returns sent alert keys."""
    d = cfg["deal"]
    day = now.astimezone(KST).date().isoformat()
    if state.get("deals_day", {}).get("d") != day:
        state["deals_day"] = {"d": day, "n": 0}
    keep = (now - timedelta(hours=d["repeat_hours"])).isoformat(timespec="seconds")
    sent = state["deals_sent"] = {k: v for k, v in state.get("deals_sent", {}).items() if v >= keep}
    groups = {}
    for x in deals:
        r = x["r"]
        if "|".join((r["kind"], r["o_ap"], r["d_ap"], r["dep_at"][:10])) in sent:
            continue
        groups.setdefault((r["kind"], r["o_ap"], r["d_ap"], r["airline"]), []).append(x)
    ranked = sorted(({"rows": v, "drop": max(x["drop"] for x in v)} for v in groups.values()), key=lambda g: -g["drop"])
    done = set()
    for g in ranked:
        if state["deals_day"]["n"] >= d["daily_cap"]:
            break
        if webhook is None:
            print("DEAL", deal_embed(g)["title"])
            continue
        try:
            post(webhook, {"embeds": [deal_embed(g, now)]})
        except OSError as e:  # rows stay regular pending candidates
            print("deal failed:", type(e).__name__, e)
            continue
        state["deals_day"]["n"] += 1
        for x in g["rows"]:
            r = x["r"]
            sent["|".join((r["kind"], r["o_ap"], r["d_ap"], r["dep_at"][:10]))] = now.isoformat(timespec="seconds")
            ak = "|".join((r["kind"], r["o_ap"], r["d_ap"], r["dep_at"][:10], r["ret_at"][:10]))
            alerted[ak] = r["price"]
            pending.pop(ak, None)
            done.add(ak)
    return done


def build_summary(rows, origins, now, min_samples=30, back_min_days=7, classes=None):
    """Cheapest fares per Japanese arrival airport (ICN+GMP merged) for the map web app."""
    classes = classes or {}
    groups = {}
    for r in rows:
        if r["o_ap"] in origins:
            groups.setdefault(r["d_ap"], {}).setdefault(r["kind"] + "_out", []).append(r)
        elif r["kind"] == "ow" and r["d_ap"] in origins:
            groups.setdefault(r["o_ap"], {}).setdefault("ow_back", []).append(r)

    def cheapest(rs):
        return min(rs, key=lambda r: r["price"])

    def ow_entry(r):
        return {"price": r["price"], "origin": r["o_ap"], "date": r["dep_at"][:10], "airline": r["airline"]}

    def rt_entry(r):
        return {"price": r["price"], "origin": r["o_ap"], "dep": r["dep_at"][:10], "ret": r["ret_at"][:10],
                "airline": r["airline"]}

    def by_class(rs, entry):  # classes with no fares are omitted
        out = {}
        for c, codes in classes.items():
            cr = [r for r in rs if r["airline"] in codes]
            if cr:
                out[c] = entry(cheapest(cr))
        return out

    earliest = (now.date() + timedelta(days=back_min_days)).isoformat()
    airports = {}
    for a, g in groups.items():
        out, rt, back = g.get("ow_out", []), g.get("rt_out", []), g.get("ow_back", [])
        if not out and not rt:
            continue
        airports[a] = {}
        if out:
            airports[a]["ow_out"] = ow_entry(cheapest(out))
        if rt:
            airports[a]["rt"] = rt_entry(cheapest(rt))
        if out:
            airports[a]["median_ow_out"] = int(statistics.median(r["price"] for r in out))
        airports[a]["samples"] = len(out)
        if len(out) < min_samples or not rt:
            airports[a]["sparse"] = True  # thin data: the web app shows these separately
        back = [r for r in back if r["dep_at"][:10] >= earliest]  # skip imminent departures
        if back:
            b = cheapest(back)
            airports[a]["ow_back"] = {"price": b["price"], "dest": b["d_ap"], "date": b["dep_at"][:10],
                                      "airline": b["airline"]}
        if classes:
            if rt:
                airports[a]["rt_by_class"] = by_class(rt, rt_entry)
            if out:
                airports[a]["ow_out_by_class"] = by_class(out, ow_entry)
    return {"updated": now.isoformat(timespec="seconds"), "source": "Travelpayouts 캐시 (참고가)",
            "airports": airports}


def build_dates(rows, origins, now, classes, airports):
    """Cheapest fare per airport/date/class for the web app's date calculator (contract: A2-SPEC S3)."""
    cls_of = {code: c for c, codes in classes.items() for code in codes}
    today = now.date().isoformat()
    out = {a: {"out": {}, "back": {}, "rt": {}} for a in airports}

    def put(a, section, k, r, kr_ap):
        c = cls_of.get(r["airline"])
        if c is None or a not in out or r["dep_at"][:10] < today:
            return  # unclassified, non-target airport, or already departed
        slot = out[a][section].setdefault(k, {})
        if c not in slot or r["price"] < slot[c][0]:
            slot[c] = [r["price"], kr_ap, r["airline"]]

    for r in rows:
        dep = r["dep_at"][:10]
        if r["kind"] == "rt" and r["o_ap"] in origins:
            put(r["d_ap"], "rt", dep + "|" + r["ret_at"][:10], r, r["o_ap"])
        elif r["kind"] == "ow" and r["o_ap"] in origins:
            put(r["d_ap"], "out", dep, r, r["o_ap"])
        elif r["kind"] == "ow" and r["d_ap"] in origins:
            put(r["o_ap"], "back", dep, r, r["d_ap"])
    return {"updated": now.isoformat(timespec="seconds"), "airports": out}


def run(cfg, token, webhook, data_dir="data", rows=None, now=None):
    now = now or datetime.now(timezone.utc)
    os.makedirs(data_dir, exist_ok=True)
    last_p, alerted_p = os.path.join(data_dir, "last.json"), os.path.join(data_dir, "alerted.json")
    pending_p, state_p = os.path.join(data_dir, "pending.json"), os.path.join(data_dir, "state.json")
    last, alerted, pending, state = read_json(last_p), read_json(alerted_p), read_json(pending_p), read_json(state_p)
    history = load_history(data_dir)

    rows = list(fetch_all(token, cfg)) if rows is None else rows
    if not rows:
        raise SystemExit("no rows fetched (token invalid or API down)")
    changed = [r for r in rows if last.get(key(r)) != r["price"]]
    ow_min = {}
    for r in rows:
        if r["kind"] == "ow":
            k = (r["o_ap"], r["d_ap"], r["dep_at"][:10])
            ow_min[k] = min(ow_min.get(k, r["price"]), r["price"])

    a = cfg["alert"]
    warm = history and history[0]["ts"] <= (now - timedelta(days=a["warmup_days"])).isoformat(timespec="seconds")
    if warm and webhook and "_warmup_notice" not in alerted:
        try:
            warmup_notice(webhook, history, a["min_samples"])
            alerted["_warmup_notice"] = 1
        except OSError as e:
            print("warmup notice failed:", type(e).__name__, e)

    sent = []
    dealt = send_deals(cfg, webhook, find_deals(changed, history, cfg, now), state, alerted, pending, now) if warm else set()
    for r in changed if warm else []:
        ak = "|".join((r["kind"], r["o_ap"], r["d_ap"], r["dep_at"][:10], r["ret_at"][:10]))
        j = None if ak in dealt else judge(r, history, cfg, now)
        if j and r["price"] <= alerted.get(ak, 1 << 60) * (1 - a["realert_drop"]):
            extra = {}
            if r["kind"] == "rt":
                extra["nights"] = (date.fromisoformat(r["ret_at"][:10]) - date.fromisoformat(r["dep_at"][:10])).days
                out = ow_min.get((r["o_ap"], r["d_ap"], r["dep_at"][:10]))
                back = ow_min.get((r["d_ap"], r["o_ap"], r["ret_at"][:10]))
                if out and back:
                    extra["ow_sum"] = out + back
            if ak not in pending or r["price"] < pending[ak]["r"]["price"]:
                found = pending.get(ak, {}).get("found_at", now.isoformat(timespec="seconds"))
                pending[ak] = {"r": r, "j": j, "extra": extra, "fkey": key(r), "found_at": found}
    prices = {key(r): r["price"] for r in rows}
    for k in [k for k, v in pending.items() if prices.get(v["fkey"], 0) > v["j"]["limit"]]:
        del pending[k]  # price rose out of the criteria
    for v in pending.values():
        if v["fkey"] in prices:
            v["r"]["price"] = prices[v["fkey"]]
    slot = due_digest(state, now, a)  # catch up a digest the scheduler missed
    if slot:
        sent += send_digest(cfg, webhook, pending, alerted, state, now, slot)

    if changed:
        path = os.path.join(data_dir, f"obs-{now:%Y-%m}.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            for r in changed:
                w.writerow({**r, "ts": now.isoformat(timespec="seconds")})
    for r in changed:
        last[key(r)] = r["price"]
    today = date.today().isoformat()
    last = {k: v for k, v in last.items() if k.split("|")[3][:10] >= today}
    write_json(last_p, last)
    write_json(alerted_p, alerted)
    write_json(pending_p, pending)
    write_json(state_p, state)
    origins, classes = cfg["search"]["origins"], cfg.get("classes")
    summary = build_summary(rows, origins, now, classes=classes)
    write_json(os.path.join(data_dir, "summary.json"), summary)
    write_json(os.path.join(data_dir, "dates.json"),
               build_dates(rows, origins, now, classes or {}, set(summary["airports"])), separators=(",", ":"))
    print(f"fetched={len(rows)} changed={len(changed)} pending={len(pending)} sent={len(sent)} deals={len(dealt)}")
    return sent


def run_digest(cfg, webhook, data_dir="data", now=None):
    """mode=digest: send the batch for the current slot from data/pending.json (no collection)."""
    now = now or datetime.now(timezone.utc)
    paths = {n: os.path.join(data_dir, n + ".json") for n in ("pending", "alerted", "state")}
    pending, alerted, state = (read_json(paths[n]) for n in ("pending", "alerted", "state"))
    slot = due_digest(state, now, cfg["alert"], force=True)
    if not slot:
        print("digest already sent for this slot")
        return []
    sent = send_digest(cfg, webhook, pending, alerted, state, now, slot)
    for n, obj in (("pending", pending), ("alerted", alerted), ("state", state)):
        write_json(paths[n], obj)
    print(f"digest sent={len(sent)} pending={len(pending)}")
    return sent


if __name__ == "__main__":
    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)
    check_config(cfg)
    if "--test" in sys.argv:  # connectivity check only, no data touched
        r = {"kind": "ow", "o_ap": "ICN", "d_ap": "NRT", "dep_at": "2026-12-01T09:00:00+09:00", "ret_at": "",
             "airline": "TEST", "price": 100000, "gate": "test", "link": ""}
        notify(os.environ["DISCORD_WEBHOOK"], [(r, {"median": 150000, "samples": 0, "drop": 0.33}, {})])
        print("test message sent")
        sys.exit(0)
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "collect"
    if mode == "digest":
        run_digest(cfg, os.environ.get("DISCORD_WEBHOOK") or None)
    elif mode == "collect":
        run(cfg, os.environ["TRAVELPAYOUTS_TOKEN"], os.environ.get("DISCORD_WEBHOOK") or None)
    else:
        sys.exit(f"unknown mode: {mode}")
