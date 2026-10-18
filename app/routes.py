"""Route planner: ranks the official PMFBY intimation channels for this case, right now.

Every choice comes with a reason string, which goes into the trace so a judge (or an
officer) can see why the agent picked a route.
"""

from . import clock

ROUTES = {
    "portal_api": {"label": "PMFBY portal / app submission", "verifiable": True, "human": False, "office_hours": False},
    "helpline_14447": {"label": "Krishi Rakshak Helpline 14447", "verifiable": False, "human": True, "office_hours": False},
    "bank_branch": {"label": "Financing bank branch", "verifiable": False, "human": True, "office_hours": True},
    "agri_officer": {"label": "Agriculture officer / Krishi Sahayak", "verifiable": False, "human": True, "office_hours": True},
}


def _office_open() -> bool:
    now = clock.now()
    return now.weekday() < 6 and 10 <= now.hour < 17


def plan(portal_healthy: bool, hours_left: float, failed: set[str]) -> list[dict]:
    """Highest score first. Scores: can it be verified automatically, is it available now,
    and how fast it is compared with the time left."""
    ranked = []
    office = _office_open()
    for key, r in ROUTES.items():
        score, reasons = 0, []
        if key in failed:
            score -= 100
            reasons.append("already failed for this case")
        if r["verifiable"]:
            score += 40
            reasons.append("receipt can be verified automatically")
        if key == "portal_api":
            if portal_healthy:
                score += 40
                reasons.append("portal health check OK")
            else:
                score -= 60
                reasons.append("portal health check FAILED")
        if r["office_hours"]:
            if office:
                score += 10
                reasons.append("office open now")
            else:
                score -= 30
                reasons.append("office closed now")
            if hours_left < 24:
                score -= 10
                reasons.append("onward forwarding may not finish in time")
        if key == "helpline_14447":
            score += 25
            reasons.append("available 24x7, gives SMS ticket")
            if hours_left < 6:
                score += 20
                reasons.append("critical time left: fastest human route")
        ranked.append({"route": key, "label": r["label"], "score": score, "reasons": reasons, "human": r["human"]})
    ranked.sort(key=lambda x: -x["score"])
    return ranked
