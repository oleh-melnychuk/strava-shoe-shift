"""Run-type helpers used by the recommender and tests."""

from __future__ import annotations

from typing import Any

RUN_TYPES = ("long", "tempo", "intervals", "recovery", "race", "unknown")
SETTABLE_RUN_TYPES = ("long", "tempo", "intervals", "recovery", "race")

# Onboarding order shown to the user.
ONBOARDING_RUN_TYPES = ("recovery", "tempo", "intervals", "long", "race")

# (label, min_km, max_km, typical_km) presets offered during onboarding.
RUN_DISTANCE_PRESETS: tuple[tuple[str, float | None, float | None, float | None], ...] = (
    ("Under 5 km", 0.0, 5.0, 4.0),
    ("5–8 km", 5.0, 8.0, 6.5),
    ("8–12 km", 8.0, 12.0, 10.0),
    ("12–16 km", 12.0, 16.0, 14.0),
    ("16–22 km", 16.0, 22.0, 19.0),
    ("22 km +", 22.0, None, 25.0),
    ("Any distance", None, None, None),
)


def next_onboarding_run_type(run_type: str) -> str | None:
    """Next run type in the onboarding wizard, or None when finished."""
    try:
        idx = ONBOARDING_RUN_TYPES.index(run_type)
    except ValueError:
        return ONBOARDING_RUN_TYPES[0]
    if idx + 1 >= len(ONBOARDING_RUN_TYPES):
        return None
    return ONBOARDING_RUN_TYPES[idx + 1]


def format_distance_profile(profile: dict[str, Any]) -> str:
    min_km = profile.get("min_km")
    max_km = profile.get("max_km")
    if min_km is None and max_km is None:
        return "any distance"
    if max_km is None:
        return f"{min_km:g} km +"
    if min_km is None or float(min_km) <= 0:
        return f"under {max_km:g} km"
    return f"{min_km:g}–{max_km:g} km"


def classify_by_user_distance(
    distance_km: float | None,
    run_type_distances: list[dict[str, Any]] | None,
) -> str | None:
    """Classify using the user's regular distance ranges from onboarding.

    Returns the run type whose range contains the distance; when ranges
    overlap, the one whose typical distance is closest wins. Profiles with
    no bounds ("Any distance") never match.
    """
    if not distance_km or distance_km <= 0 or not run_type_distances:
        return None
    matches: list[tuple[float, str]] = []
    for profile in run_type_distances:
        run_type = profile.get("run_type")
        if run_type not in SETTABLE_RUN_TYPES:
            continue
        min_km = profile.get("min_km")
        max_km = profile.get("max_km")
        if min_km is None and max_km is None:
            continue
        if min_km is not None and distance_km < float(min_km):
            continue
        if max_km is not None and distance_km > float(max_km):
            continue
        typical = profile.get("typical_km")
        if typical is None:
            lo = float(min_km) if min_km is not None else distance_km
            hi = float(max_km) if max_km is not None else distance_km
            typical = (lo + hi) / 2.0
        matches.append((abs(float(typical) - distance_km), str(run_type)))
    if not matches:
        return None
    matches.sort()
    return matches[0][1]


def normalize_run_type(value: str | None) -> str:
    if not value:
        return "unknown"
    cleaned = value.strip().lower()
    return cleaned if cleaned in RUN_TYPES else "unknown"


def classify_heuristic(
    activity: dict[str, Any],
    run_type_distances: list[dict[str, Any]] | None = None,
) -> str:
    name = (activity.get("name") or "").lower()
    description = (activity.get("description") or "").lower()
    text = f"{name} {description}"
    workout_type = activity.get("workout_type")
    distance = float(activity.get("distance") or 0)
    distance_km = distance / 1000.0 if distance else None

    if "race" in text or workout_type == 1:
        return "race"
    if any(k in text for k in ("interval", "track", "repeats", "vo2")):
        return "intervals"
    if any(k in text for k in ("tempo", "threshold", "cruise")) or workout_type == 3:
        return "tempo"
    if any(k in text for k in ("recovery", "easy", "shakeout")):
        return "recovery"
    if "long" in text or workout_type == 2:
        return "long"
    # User's own regular distances (onboarding) beat generic distance rules.
    by_user = classify_by_user_distance(distance_km, run_type_distances)
    if by_user:
        return by_user
    if distance >= 16000:
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
