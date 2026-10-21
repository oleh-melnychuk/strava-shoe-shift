"""Run-type helpers used by the recommender and tests."""

from __future__ import annotations

from typing import Any

RUN_TYPES = ("long", "tempo", "intervals", "recovery", "race", "unknown")
SETTABLE_RUN_TYPES = ("long", "tempo", "intervals", "recovery", "race")


def normalize_run_type(value: str | None) -> str:
    if not value:
        return "unknown"
    cleaned = value.strip().lower()
    return cleaned if cleaned in RUN_TYPES else "unknown"


def classify_heuristic(activity: dict[str, Any]) -> str:
    name = (activity.get("name") or "").lower()
    description = (activity.get("description") or "").lower()
    text = f"{name} {description}"
    workout_type = activity.get("workout_type")
    distance = float(activity.get("distance") or 0)

    if "race" in text or workout_type == 1:
        return "race"
    if any(k in text for k in ("interval", "track", "repeats", "vo2")):
        return "intervals"
    if any(k in text for k in ("tempo", "threshold", "cruise")) or workout_type == 3:
        return "tempo"
    if any(k in text for k in ("recovery", "easy", "shakeout")):
        return "recovery"
    if "long" in text or workout_type == 2 or distance >= 16000:
        return "long"
    if distance and distance < 8000:
        return "recovery"
    return "unknown"


def activity_summary(activity: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": activity.get("name"),
        "description": activity.get("description"),
        "type": activity.get("type") or activity.get("sport_type"),
        "workout_type": activity.get("workout_type"),
        "distance_m": activity.get("distance"),
        "moving_time_s": activity.get("moving_time"),
        "average_speed_mps": activity.get("average_speed"),
        "average_heartrate": activity.get("average_heartrate"),
    }
