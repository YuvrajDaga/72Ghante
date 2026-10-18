"""Dashboard aggregates computed from the case store (live + seeded demo cases)."""

from collections import Counter
from datetime import timedelta

from . import clock, store


def _created(c):
    return clock.parse(c.get("created"))


def _verified_at(c):
    return clock.parse(c["intimation"].get("verified_at"))


def _hours_left(c):
    dl = clock.parse(c.get("deadline"))
    return (dl - clock.now()).total_seconds() / 3600 if dl else None


def _submitted(c):
    return bool(c.get("attempts")) or c["intimation"]["status"] in ("verified", "submitted_unverified", "pending")


def _in_progress(c):
    return c["stage"] in ("intake", "confirm", "filing") and c["intimation"]["status"] != "verified"


def _at_risk(c):
    h = _hours_left(c)
    return _in_progress(c) and h is not None and 0 < h < 12


def _acknowledged(c):
    return c["intimation"]["status"] == "verified"


def _trend(cases, pred, now):
    """% change of cases matching pred created in the last 7 days vs the 7 days before."""
    cur = sum(1 for c in cases if pred(c) and _created(c) and now - _created(c) <= timedelta(days=7))
    prev = sum(1 for c in cases if pred(c) and _created(c) and timedelta(days=7) < now - _created(c) <= timedelta(days=14))
    return round(100 * (cur - prev) / prev) if prev else 0


def _spark(cases, pred, now, days=12):
    out = []
    for d in range(days - 1, -1, -1):
        day = (now - timedelta(days=d)).date()
        out.append(sum(1 for c in cases if pred(c) and _created(c) and _created(c).date() == day))
    return out


def compute() -> dict:
    every = store.list_cases()
    # Test-scenario runs are verification artifacts, not cases: keep them out of the analytics.
    cases = [c for c in every if not c.get("scenario")]
    now = clock.now()
    resolution = [(_verified_at(c) - _created(c)).total_seconds() / 60 for c in cases if _acknowledged(c) and _verified_at(c) and _created(c)]
    avg_res = sum(resolution) / len(resolution) if resolution else 0
    prev_res = [(_verified_at(c) - _created(c)).total_seconds() / 60 for c in cases
                if _acknowledged(c) and _verified_at(c) and _created(c) and timedelta(days=7) < now - _created(c) <= timedelta(days=14)]
    cur_res = [(_verified_at(c) - _created(c)).total_seconds() / 60 for c in cases
               if _acknowledged(c) and _verified_at(c) and _created(c) and now - _created(c) <= timedelta(days=7)]
    res_trend = round(100 * ((sum(cur_res) / len(cur_res)) - (sum(prev_res) / len(prev_res))) / (sum(prev_res) / len(prev_res))) if cur_res and prev_res else 0

    kpis = {
        "total": {"value": len(cases), "trend": _trend(cases, lambda c: True, now), "spark": _spark(cases, lambda c: True, now)},
        "submitted": {"value": sum(map(_submitted, cases)), "trend": _trend(cases, _submitted, now), "spark": _spark(cases, _submitted, now)},
        "in_progress": {"value": sum(map(_in_progress, cases)), "trend": _trend(cases, _in_progress, now), "spark": _spark(cases, _in_progress, now)},
        "at_risk": {"value": sum(map(_at_risk, cases)), "trend": _trend(cases, _at_risk, now), "spark": _spark(cases, _in_progress, now)},
        "acknowledged": {"value": sum(map(_acknowledged, cases)), "trend": _trend(cases, _acknowledged, now), "spark": _spark(cases, _acknowledged, now)},
        "avg_resolution_min": {"value": round(avg_res, 1), "trend": res_trend, "spark": _spark(cases, _acknowledged, now)},
    }

    daily = []
    for d in range(17, -1, -1):
        day = (now - timedelta(days=d)).date()
        todays = [c for c in cases if _created(c) and _created(c).date() == day]
        daily.append({"date": day.isoformat(), "total": len(todays), "submitted": sum(map(_submitted, todays)),
                      "in_progress": sum(map(_in_progress, todays)), "at_risk": sum(1 for c in todays if c["stage"] == "escalated" or _at_risk(c)),
                      "acknowledged": sum(map(_acknowledged, todays))})
    # 7-day forecast: average of the last 7 days, extended with the recent slope.
    last = [d["total"] for d in daily[-7:]]
    slope = (last[-1] - last[0]) / 6 if len(last) == 7 else 0
    base = sum(last) / len(last) if last else 0
    forecast = [{"date": (now + timedelta(days=i)).date().isoformat(), "total": max(0, round(base + slope * (i + 3)))} for i in range(1, 8)]

    def by(key):
        return Counter(key(c) for c in cases if key(c)).most_common()

    crops = by(lambda c: c["fields"].get("crop"))
    crop_trend = {k: _trend(cases, lambda c, k=k: c["fields"].get("crop") == k, now) for k, _ in crops}

    talukas = by(lambda c: c["fields"].get("taluka") or c["fields"].get("village"))
    taluka_trend = {k: _trend(cases, lambda c, k=k: (c["fields"].get("taluka") or c["fields"].get("village")) == k, now) for k, _ in talukas}
    taluka_district = {}
    for c in cases:
        t = c["fields"].get("taluka") or c["fields"].get("village")
        if t:
            taluka_district[t] = c["fields"].get("district")

    bands = Counter()
    for m in resolution:
        bands["< 15 min" if m < 15 else "15–30 min" if m < 30 else "30–60 min" if m < 60 else "1–4 h" if m < 240 else "> 4 h"] += 1

    reasons = Counter()
    for c in cases:
        r = c.get("failure_reason")
        if not r:
            errs = [a.get("error") for a in c.get("attempts", []) if a.get("result") == "failed"]
            if errs:
                r = "API timeout" if errs[0] == "timeout" else "Server error"
            elif any(e.get("type") == "damage_photo" and not e.get("accepted") for e in c.get("evidence", [])):
                r = "Invalid document"
            elif any(a.get("result") == "duplicate" for a in c.get("attempts", [])):
                r = "Duplicate request"
        if r:
            reasons[r] += 1

    failed_attempts = [c for c in cases if any(a.get("result") == "failed" for a in c.get("attempts", []))]
    return {
        "kpis": kpis,
        "live": sum(1 for c in cases if not c.get("demo")),
        "sources": {"live": sum(1 for c in cases if not c.get("demo")), "demo": sum(1 for c in cases if c.get("demo")),
                    "test_runs_excluded": sum(1 for c in every if c.get("scenario"))},
        "daily": daily, "forecast": forecast,
        "crops": [{"crop": k, "n": v, "trend": crop_trend[k]} for k, v in crops],
        "talukas": [{"taluka": k, "district": taluka_district.get(k), "n": v, "trend": taluka_trend[k]} for k, v in talukas],
        "resolution_bands": [[k, bands.get(k, 0)] for k in ("< 15 min", "15–30 min", "30–60 min", "1–4 h", "> 4 h")],
        "success": {"successful": sum(map(_acknowledged, cases)), "failed": sum(1 for c in cases if c["stage"] == "escalated"),
                    "pending": sum(map(_in_progress, cases))},
        "failure_reasons": reasons.most_common(),
        "recovery": {"failures": len(failed_attempts), "recovered": sum(1 for c in failed_attempts if _acknowledged(c)),
                     "false_done": sum(1 for c in cases if _acknowledged(c) and not c["intimation"].get("reference"))},
        "within_window": sum(1 for c in cases if _acknowledged(c) and _verified_at(c) and clock.parse(c.get("deadline")) and _verified_at(c) <= clock.parse(c["deadline"])),
        "channels": by(lambda c: c.get("channel")),
        "languages": by(lambda c: c.get("language")),
    }
