"""The 72-hour clock: which loss regime applies, when the window closes, and how urgent the case is.

This is plain deterministic code on purpose. The LLM extracts what the farmer said; the
rules about deadlines are never left to the LLM.
"""

from datetime import datetime, timedelta

from . import clock
from .config import INTIMATION_WINDOW_HOURS

# PMFBY individual-farm (72-hour) intimation applies to localised calamities and post-harvest losses.
LOCALISED = {"hailstorm", "landslide", "inundation", "cloudburst", "natural_fire", "lightning_fire"}
POST_HARVEST = {"cyclone", "cyclonic_rain", "unseasonal_rain"}
# Widespread losses are assessed on an area basis (yield estimation), not by individual intimation.
WIDESPREAD = {"drought", "dry_spell", "widespread_flood", "pest_attack", "disease"}

POST_HARVEST_DAYS = 14


def classify_peril(peril: str | None, crop_harvested: bool | None) -> str:
    """Returns localised | post_harvest | widespread | needs_info | unknown."""
    if not peril:
        return "unknown"
    if peril in LOCALISED:
        return "localised"
    if peril in POST_HARVEST:
        # Rain or cyclone damage counts individually only for a harvested crop drying in the field.
        if crop_harvested is None:
            return "needs_info"
        return "post_harvest" if crop_harvested else "widespread"
    if peril in WIDESPREAD:
        return "widespread"
    return "unknown"


def deadline_for(event_time: datetime) -> datetime:
    return event_time + timedelta(hours=INTIMATION_WINDOW_HOURS)


def hours_left(deadline: datetime) -> float:
    return (deadline - clock.now()).total_seconds() / 3600


def urgency(hours: float) -> str:
    """How the agent behaves: normal asks for everything; hurry files first and collects photos
    after; critical files immediately and opens a parallel human route; expired escalates."""
    if hours <= 0:
        return "expired"
    if hours < 6:
        return "critical"
    if hours < 24:
        return "hurry"
    return "normal"


def describe(hours: float) -> str:
    if hours <= 0:
        return "window closed"
    h = int(hours)
    m = int((hours - h) * 60)
    return f"{h}h {m:02d}m left"
