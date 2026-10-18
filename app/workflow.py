"""Case status rules. Plain code, so a model can never move a case into a state it has not earned."""

# stage -> stages it may move to
TRANSITIONS = {
    "intake": {"confirm", "out_of_scope", "escalated"},
    "confirm": {"intake", "filing", "escalated"},
    "filing": {"tracking", "escalated", "intake"},
    "tracking": {"closed", "escalated"},
    "escalated": {"filing", "tracking", "closed"},
    "out_of_scope": {"intake"},
    "closed": set(),
}

LABELS = {
    "intake": "Collecting information", "confirm": "Awaiting consent", "filing": "Submitting",
    "tracking": "Acknowledged, tracking surveyor", "escalated": "Escalated to human", "out_of_scope": "Area-based (no 72h report)",
    "closed": "Closed",
}


def check_transition(case: dict, to: str) -> str | None:
    """Returns an error message, or None if the move is allowed."""
    frm = case["stage"]
    if to not in TRANSITIONS:
        return f"Unknown stage '{to}'"
    if to not in TRANSITIONS.get(frm, set()):
        return f"Cannot move from '{frm}' to '{to}'"
    if to == "filing" and not case.get("consent_at"):
        return "Explicit farmer consent is required before filing"
    if to == "tracking" and case["intimation"]["status"] != "verified":
        return "Only a verified docket can move a case to tracking"
    if to == "closed" and frm == "escalated" and case["intimation"]["status"] != "verified":
        return "An escalated case can only close after a verified docket"
    return None


def milestones(case: dict) -> list[dict]:
    """The six milestones the dashboard shows, each with its evidence timestamp (or None)."""
    att = case.get("attempts", [])
    first_attempt = att[0]["at"] if att else None
    accepted = next((a["at"] for a in att if a.get("result") in ("accepted", "duplicate")), None)
    info_done = case["stage"] not in ("intake",) or case["intimation"]["status"] == "verified"
    return [
        {"key": "received", "label": "Report received", "at": case.get("created")},
        {"key": "extracted", "label": "Information extracted", "at": case.get("info_complete_at") if info_done else None, "done": info_done},
        {"key": "consent", "label": "Consent obtained", "at": case.get("consent_at")},
        {"key": "attempted", "label": "Submission attempted", "at": first_attempt},
        {"key": "confirmed", "label": "Submission confirmed", "at": accepted},
        {"key": "verified", "label": "Acknowledgement verified", "at": case["intimation"].get("verified_at")},
    ]
