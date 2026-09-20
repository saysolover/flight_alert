"""Collect Travelpayouts cached fares, store price changes, alert Discord on significant lows."""
import csv, glob, json, os, time, tomllib, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
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


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


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
    if row["price"] > percentile(prices, a["percentile"]) or row["price"] > median * (1 - a["min_drop"]):
        return None
    same = [h["price"] for h in series if h["dep_at"][:10] == row["dep_at"][:10] and h["ret_at"][:10] == row["ret_at"][:10]]
    if same and row["price"] >= min(same):
        return None  # not a new low for this departure
    return {"median": median, "samples": len(prices), "drop": 1 - row["price"] / median}


def notify(webhook, alerts):
    embeds = []
    for r, j, extra in alerts:
        kind = f"왕복 {extra['nights']}박" if r["kind"] == "rt" else "편도"
        desc = (f"**{r['price']:,}원** ({r['airline']}, {r['gate']})\n"
                f"최근 {j['samples']}건 중앙값 {j['median']:,}원 대비 -{j['drop']:.0%}\n"
                f"출발 {r['dep_at'][:16]}" + (f" / 귀국 {r['ret_at'][:16]}" if r["ret_at"] else ""))
        if extra.get("ow_sum"):
            desc += f"\n편도 합산 {extra['ow_sum']:,}원"
        desc += "\n_캐시 기반 참고가입니다. 예약 전 재확인하세요._"
        embed = {"title": f"{r['o_ap']} → {r['d_ap']} · {kind}", "description": desc}
        if r["link"]:
            embed["url"] = "https://www.aviasales.com" + r["link"]
        embeds.append(embed)
    for i in range(0, len(embeds), 10):
        body = json.dumps({"embeds": embeds[i:i + 10]}).encode()
        req = urllib.request.Request(webhook, body, {"Content-Type": "application/json", "User-Agent": "flight-alert"})
        urllib.request.urlopen(req, timeout=30).close()


def run(cfg, token, webhook, data_dir="data", rows=None):
    now = datetime.now(timezone.utc)
    os.makedirs(data_dir, exist_ok=True)
    last_p, alerted_p = os.path.join(data_dir, "last.json"), os.path.join(data_dir, "alerted.json")
    last = read_json(last_p)
    alerted = read_json(alerted_p)
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

    alerts = []
    warm = history and history[0]["ts"] <= (now - timedelta(days=cfg["alert"]["warmup_days"])).isoformat(timespec="seconds")
    for r in changed if warm else []:
        j = judge(r, history, cfg, now)
        ak = "|".join((r["kind"], r["o_ap"], r["d_ap"], r["dep_at"][:10], r["ret_at"][:10]))
        if j and r["price"] <= alerted.get(ak, 1 << 60) * (1 - cfg["alert"]["realert_drop"]):
            extra = {}
            if r["kind"] == "rt":
                extra["nights"] = (date.fromisoformat(r["ret_at"][:10]) - date.fromisoformat(r["dep_at"][:10])).days
                out = ow_min.get((r["o_ap"], r["d_ap"], r["dep_at"][:10]))
                back = ow_min.get((r["d_ap"], r["o_ap"], r["ret_at"][:10]))
                if out and back:
                    extra["ow_sum"] = out + back
            alerts.append((r, j, extra, ak))
    alerts.sort(key=lambda a: -a[1]["drop"])
    alerts = alerts[:cfg["alert"]["max_alerts_per_run"]]

    if alerts:
        if webhook:
            try:
                notify(webhook, [a[:3] for a in alerts])
                for r, _, _, ak in alerts:
                    alerted[ak] = r["price"]
            except OSError as e:  # keep collecting data even if Discord is down
                print("notify failed:", type(e).__name__, e)
        else:
            for r, j, _, _ in alerts:
                print("ALERT", r["o_ap"], r["d_ap"], r["dep_at"][:10], r["price"], f"-{j['drop']:.0%}")

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
    print(f"fetched={len(rows)} changed={len(changed)} alerts={len(alerts)}")
    return alerts


if __name__ == "__main__":
    with open("config.toml", "rb") as f:
        cfg = tomllib.load(f)
    run(cfg, os.environ["TRAVELPAYOUTS_TOKEN"], os.environ.get("DISCORD_WEBHOOK"))
