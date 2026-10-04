#!/usr/bin/env python3
"""매일 인천발 항공권 최저가를 찾아 텔레그램으로 알려주는 에이전트 (SerpApi + Google Flights)."""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys

import requests
import yaml

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"  # GitHub Pages가 서비스하는 폴더
SERP = "https://serpapi.com/search.json"
BANDS = {
    "night": range(0, 6),
    "morning": range(6, 12),
    "afternoon": range(12, 18),
    "evening": range(18, 24),
}
WEEKDAYS = "월화수목금토일"


# ---------- 유틸 ----------
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def to_int(v):
    if isinstance(v, (int, float)):
        return int(v)
    digits = "".join(c for c in str(v or "") if c.isdigit())
    return int(digits) if digits else None


def fmt_date(d):
    return f"{d:%m/%d}({WEEKDAYS[d.weekday()]})"


# ---------- 설정 해석 ----------
def resolve_airlines(cfg):
    groups = {**(cfg.get("alliances") or {}), **(cfg.get("airline_groups") or {})}
    codes = set()
    for name in cfg.get("airlines") or []:
        if name in groups:
            codes |= {c.upper() for c in groups[name]}
        elif len(str(name)) == 2 and str(name).isalnum():
            codes.add(str(name).upper())
        else:
            sys.exit(f"알 수 없는 항공사/그룹 이름: {name} (airline_groups나 alliances에 정의하세요)")
    return codes


def resolve_hours(names):
    if not names:
        return set(range(24))
    hours = set()
    for n in names:
        if n not in BANDS:
            sys.exit(f"알 수 없는 시간대: {n} (night/morning/afternoon/evening)")
        hours |= set(BANDS[n])
    return hours


# ---------- 호출 한도 ----------
class Budget:
    def __init__(self, cfg, today):
        s = load_json(DATA / "usage.json", {})
        if s.get("month") != today.strftime("%Y-%m"):
            s = {"month": today.strftime("%Y-%m"), "month_count": 0}
        if s.get("day") != str(today):
            s["day"], s["day_count"] = str(today), 0
        self.s = s
        self.day_limit = cfg.get("daily_call_limit", 8)
        self.month_limit = cfg.get("monthly_call_limit", 230)

    def ok(self):
        return self.s["day_count"] < self.day_limit and self.s["month_count"] < self.month_limit

    def spend(self):
        self.s["day_count"] += 1
        self.s["month_count"] += 1

    def save(self):
        save_json(DATA / "usage.json", self.s)


def serp(params, budget):
    if not budget.ok():
        print("호출 한도에 도달해 조회를 건너뜁니다.")
        return None
    budget.spend()
    r = requests.get(SERP, params={**params, "api_key": os.environ["SERPAPI_KEY"]}, timeout=90)
    if r.status_code != 200:
        print(f"SerpApi 오류 {r.status_code}: {r.text[:200]}")
        return None
    return r.json()


# ---------- 일정 선택 ----------
def pick_trip(s, today):
    a, b = (dt.date.fromisoformat(x.strip()) for x in str(s["depart"]).split(".."))
    a = max(a, today + dt.timedelta(days=1))
    if b < a:
        return None
    n = (b - a).days + 1
    k = today.toordinal()
    d = a + dt.timedelta(days=k % n)
    stay = s["stay_days"][(k // n) % len(s["stay_days"])]
    return d, d + dt.timedelta(days=stay)


# ---------- 파싱/필터 ----------
def _walk(o, path=""):
    """중첩 dict/list를 (키경로, 값)으로 펼친다."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _walk(v, f"{path}.{i}")
    else:
        yield path.lower(), o


def explore_candidates(data, cfg):
    exclude = {c.upper() for c in cfg.get("exclude_destinations") or []}
    out = []
    for x in data.get("destinations", []):
        flat = list(_walk(x))
        price = next((to_int(v) for k, v in flat if "price" in k and "cost_per" not in k and to_int(v)), None)
        code = next((str(v).upper() for k, v in flat
                     if ("airport" in k or k.endswith("code")) and "airline" not in k
                     and isinstance(v, str) and len(v) == 3 and v.isalpha()), None)
        name = x.get("name", code) if isinstance(x, dict) else code
        if code and price and code not in exclude:
            out.append((price, code, name))
    return sorted(out)


def seg_airline(seg):
    fn = str(seg.get("flight_number", "")).strip()
    return fn.split()[0].upper() if fn else ""


def hour_of(t):  # "2026-12-01 09:05"
    return int(str(t)[11:13])


def matches(it, codes, dep_hours, arr_hours):
    segs = it.get("flights") or []
    if not segs:
        return False
    if codes and not all(seg_airline(s) in codes for s in segs):
        return False
    try:
        return (hour_of(segs[0]["departure_airport"]["time"]) in dep_hours
                and hour_of(segs[-1]["arrival_airport"]["time"]) in arr_hours)
    except (KeyError, ValueError):
        return False


# ---------- 알림 ----------
def send(text, dry):
    print(text, "\n")
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if dry or not (token and chat):
        return
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )


def skyscanner_url(origin, code, out_d, ret_d):
    return (f"https://www.skyscanner.co.kr/transport/flights/"
            f"{origin.lower()}/{code.lower()}/{out_d:%y%m%d}/{ret_d:%y%m%d}/")


def build_message(origin, code, name, price, out_d, ret_d, it, prev_min, google_url):
    segs = it["flights"]
    stops = len(segs) - 1
    airlines = "/".join(dict.fromkeys(seg_airline(s) for s in segs))
    lines = [
        f"✈️ {origin}→{name}({code}) {price:,}원",
        f"{fmt_date(out_d)}→{fmt_date(ret_d)} · {airlines} · {'직항' if stops == 0 else f'경유 {stops}회'}",
        f"가는 편 {str(segs[0]['departure_airport']['time'])[11:16]} → {str(segs[-1]['arrival_airport']['time'])[11:16]}",
    ]
    if prev_min:
        lines.append(f"이 목적지 기록 최저가 갱신 (이전 {prev_min:,}원)")
    if google_url:
        lines.append(f"구글: {google_url}")
    lines.append(f"스카이스캐너: {skyscanner_url(origin, code, out_d, ret_d)}")
    return "\n".join(lines)


# ---------- 메인 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="텔레그램 전송 없이 출력만")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    origin, max_price = cfg["origin"], cfg["max_price"]
    codes = resolve_airlines(cfg)
    dep_h = resolve_hours((cfg.get("time_bands") or {}).get("depart"))
    arr_h = resolve_hours((cfg.get("time_bands") or {}).get("arrive"))
    targets = {k.upper(): v for k, v in (cfg.get("target_prices") or {}).items()}
    budget = Budget(cfg, today)
    hist = load_json(DATA / "history.json", {})
    alerts = []
    res = load_json(DOCS / "results.json", {"rows": []})
    cutoff = str(today - dt.timedelta(days=90))
    rows = [r for r in res["rows"] if r["seen"] >= cutoff]
    k = today.toordinal()

    for s in cfg["schedules"]:
        trip = pick_trip(s, today)
        if not trip:
            continue
        out_d, ret_d = trip
        base = {"hl": "ko", "gl": "kr", "currency": cfg["currency"],
                "outbound_date": out_d.isoformat(), "return_date": ret_d.isoformat()}
        if cfg.get("nonstop_only"):
            base["stops"] = 1

        if cfg["destinations"] == "any":
            data = serp({**base, "engine": "google_travel_explore",
                         "departure_id": origin, "max_price": max_price}, budget)
            if not data:
                continue
            cands = explore_candidates(data, cfg)
            if not cands:
                dl = data.get("destinations") or []
                print(f"익스플로어 후보 없음. 응답 키: {list(data.keys())}, destinations {len(dl)}개")
                if dl:
                    print("첫 항목:", json.dumps(dl[0], ensure_ascii=False)[:700])
                else:
                    print("검색 조건:", json.dumps(data.get("search_parameters"), ensure_ascii=False)[:400])
        else:
            dests = [d.upper() for d in cfg["destinations"]]
            r = k % len(dests)
            cands = [(0, d, d) for d in dests[r:] + dests[:r]]

        for _, code, name in cands[: cfg.get("detail_per_schedule", 3)]:
            data = serp({**base, "engine": "google_flights", "departure_id": origin,
                         "arrival_id": code, "type": 1, "max_price": max_price}, budget)
            if not data:
                continue
            best = None
            for it in (data.get("best_flights") or []) + (data.get("other_flights") or []):
                p = to_int(it.get("price"))
                if p and matches(it, codes, dep_h, arr_h) and (best is None or p < best[0]):
                    best = (p, it)
            if not best or best[0] > max_price:
                print(f"{code}: 조건에 맞는 항공편 없음")
                continue
            price, it = best
            segs = it["flights"]
            url = (data.get("search_metadata") or {}).get("google_flights_url")
            row = {"seen": str(today), "dest": code, "name": name, "price": price,
                   "out": str(out_d), "ret": str(ret_d),
                   "airlines": list(dict.fromkeys(seg_airline(x) for x in segs)),
                   "stops": len(segs) - 1,
                   "dep": str(segs[0]["departure_airport"]["time"])[11:16],
                   "arr": str(segs[-1]["arrival_airport"]["time"])[11:16],
                   "google": url, "sky": skyscanner_url(origin, code, out_d, ret_d)}
            rows = [r for r in rows if (r["seen"], r["dest"], r["out"], r["ret"]) !=
                    (row["seen"], code, row["out"], row["ret"])] + [row]

            rec = hist.setdefault(code, {"min": None, "alerted": None, "log": []})
            prev_min = rec["min"]
            new_low = prev_min is None or price < prev_min
            hit_target = price <= targets.get(code, 0)
            if (new_low or hit_target) and price != rec["alerted"]:
                alerts.append(build_message(origin, code, name, price, out_d, ret_d, it,
                                            prev_min if new_low else None, url))
                rec["alerted"] = price
            rec["min"] = price if prev_min is None else min(prev_min, price)
            rec["log"] = (rec["log"] + [[str(today), price, str(out_d), str(ret_d)]])[-90:]

    for a in alerts:
        send(a, args.dry_run)
    if not alerts and cfg.get("notify_when_nothing"):
        send("오늘은 새로 알릴 만한 항공권이 없어요.", args.dry_run)

    save_json(DATA / "history.json", hist)
    save_json(DOCS / "results.json", {"updated": str(today), "origin": origin,
                                      "currency": cfg["currency"], "rows": rows})
    budget.save()
    print(f"오늘 호출 {budget.s['day_count']}회 / 이번 달 {budget.s['month_count']}회, 알림 {len(alerts)}건")


if __name__ == "__main__":
    main()
