from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from app.ai.classifier import classify_by_user_distance, classify_heuristic
from app.config import get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You assign running shoes for Strava activities.

Classify the run type as one of: long, tempo, intervals, recovery, race, unknown.
Then pick the best shoe_id from the provided shoe list.

Primary evidence:
- explicit_preferences: shoes the user pinned via /prefer for a run type. When your
  classified run_type matches a pin, you MUST pick that pinned shoe.
- recent_activities: the athlete's last ~50 runs with name, distance, pace, HR, gear, and
  an inferred_run_type hint. Find which shoes they actually used for similar efforts.
- usage_trends: aggregates from those recent runs (by_run_type counts per shoe).
- user_run_type_distances: the user's regular distance range per run type from
  onboarding (min_km/max_km/typical_km). Use it to classify when name/pace cues
  are ambiguous — e.g. their 18 km run is a long run, their 6 km run is recovery.

Rules:
- Never invent a shoe_id; only use ids from the shoes list.
- Match the new activity to similar recent runs (pace, HR, distance, name cues), then
  pick the shoe most often used on those similar runs.
- Do NOT copy activity.current_gear_id just because it is already set — it may be wrong.
- Avoid retired shoes unless no alternative exists.
- Prefer lower-mileage shoes for intervals/race only when recent history is inconclusive.
- If history is ambiguous, lower confidence and still propose your best guess.
- In reason, cite concrete recent-run evidence (e.g. "3 of 4 easy/recovery runs used X").
- Respond with JSON only matching the schema.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "run_type": {
            "type": "string",
            "enum": ["long", "tempo", "intervals", "recovery", "race", "unknown"],
        },
        "shoe_id": {"type": "string", "nullable": True},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["run_type", "confidence", "reason"],
}


class ShoeRecommendation(BaseModel):
    run_type: str = Field(description="long|tempo|intervals|recovery|race|unknown")
    shoe_id: str | None = Field(default=None, description="Selected shoe id from the list")
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


def _pace_min_per_km(distance_m: float | None, moving_time_s: float | None) -> float | None:
    if not distance_m or distance_m <= 0 or not moving_time_s:
        return None
    return round((float(moving_time_s) / 60.0) / (float(distance_m) / 1000.0), 2)


def build_recent_activity_context(
    activities: list[dict[str, Any]],
    shoe_names: dict[str, str],
    *,
    limit: int = 50,
    run_type_distances: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Normalize the last N runs for the recommender."""
    rows: list[dict[str, Any]] = []
    for act in activities:
        if (act.get("type") or act.get("sport_type")) not in {"Run", "TrailRun", "VirtualRun"}:
            continue
        gear_id = act.get("gear_id")
        rows.append(
            {
                "id": act.get("id"),
                "name": act.get("name"),
                "start_date": act.get("start_date_local") or act.get("start_date"),
                "distance_m": act.get("distance"),
                "distance_km": round(float(act.get("distance") or 0) / 1000.0, 2),
                "moving_time_s": act.get("moving_time"),
                "pace_min_per_km": _pace_min_per_km(act.get("distance"), act.get("moving_time")),
                "average_heartrate": act.get("average_heartrate"),
                "max_heartrate": act.get("max_heartrate"),
                "workout_type": act.get("workout_type"),
                "gear_id": gear_id,
                "gear_name": shoe_names.get(gear_id) if gear_id else None,
                "inferred_run_type": classify_heuristic(act, run_type_distances),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _preference_supported(
    run_type: str, shoe_id: str | None, preferences: list[dict[str, Any]]
) -> bool:
    if not shoe_id:
        return False
    return any(
        p.get("run_type") == run_type
        and p.get("shoe_id") == shoe_id
        and int(p.get("accept_count") or 0) > int(p.get("reject_count") or 0)
        for p in preferences
    )


def _pinned_shoe(
    run_type: str, preferences: list[dict[str, Any]]
) -> dict[str, Any] | None:
    pins = [
        p
        for p in preferences
        if p.get("run_type") == run_type
        and int(p.get("accept_count") or 0) > int(p.get("reject_count") or 0)
    ]
    if not pins:
        return None
    pins.sort(
        key=lambda p: (
            int(p.get("accept_count") or 0) - int(p.get("reject_count") or 0),
            p.get("updated_at") or "",
        ),
        reverse=True,
    )
    return pins[0]


def _infer_run_type_from_similar(
    activity: dict[str, Any],
    recent_activities: list[dict[str, Any]],
    preferences: list[dict[str, Any]],
) -> str | None:
    """If AI says unknown, infer type from similar recent runs whose shoe is pinned."""
    target_pace = _pace_min_per_km(activity.get("distance"), activity.get("moving_time"))
    if target_pace is None:
        return None
    target_hr = activity.get("average_heartrate")
    target_km = float(activity.get("distance") or 0) / 1000.0

    similar_gears: list[str] = []
    for row in recent_activities:
        pace = row.get("pace_min_per_km")
        if pace is None or abs(float(pace) - target_pace) > 0.30:
            continue
        hr = row.get("average_heartrate")
        if target_hr and hr and abs(float(hr) - float(target_hr)) > 10:
            continue
        km = float(row.get("distance_km") or 0)
        if target_km and km and abs(km - target_km) > 4.0:
            continue
        if row.get("gear_id"):
            similar_gears.append(str(row["gear_id"]))

    if len(similar_gears) < 2:
        return None
    most_common_id, count = Counter(similar_gears).most_common(1)[0]
    if count < 2:
        return None
    for pref in preferences:
        if pref.get("shoe_id") == most_common_id and int(
            pref.get("accept_count") or 0
        ) > int(pref.get("reject_count") or 0):
            return str(pref.get("run_type"))
    return None


def _build_user_payload(
    activity: dict[str, Any],
    shoes: list[dict[str, Any]],
    recent_activities: list[dict[str, Any]],
    usage_trends: list[dict[str, Any]],
    preferences: list[dict[str, Any]] | None = None,
    run_type_distances: list[dict[str, Any]] | None = None,
) -> str:
    payload = {
        "activity": {
            "name": activity.get("name"),
            "description": activity.get("description"),
            "type": activity.get("type") or activity.get("sport_type"),
            "workout_type": activity.get("workout_type"),
            "distance_m": activity.get("distance"),
            "moving_time_s": activity.get("moving_time"),
            "elapsed_time_s": activity.get("elapsed_time"),
            "pace_min_per_km": _pace_min_per_km(activity.get("distance"), activity.get("moving_time")),
            "average_speed_mps": activity.get("average_speed"),
            "max_speed_mps": activity.get("max_speed"),
            "average_heartrate": activity.get("average_heartrate"),
            "max_heartrate": activity.get("max_heartrate"),
            "current_gear_id": activity.get("gear_id"),
            "note": "Do not trust current_gear_id; decide from recent_activities.",
        },
        "shoes": shoes,
        "explicit_preferences": preferences or [],
        "user_run_type_distances": run_type_distances or [],
        "recent_activities": recent_activities,
        "usage_trends": usage_trends,
        "instruction": (
            "Classify this activity. If explicit_preferences has a pin for that "
            "run_type, pick the pinned shoe. Otherwise find similar rows in "
            "recent_activities and pick the shoe those similar runs used most."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.ServerError):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return status in {429, 500, 502, 503, 504}


# Models that recently returned 429/503 are skipped for a while instead of
# being hammered on every activity. The last model that worked is tried first.
_MODEL_COOLDOWN: dict[str, float] = {}
_MODEL_COOLDOWN_SECONDS = 120.0
_LAST_GOOD_MODEL: str | None = None
# At/above this confidence the local history/pin pick is used and Gemini is
# not called at all.
HEURISTIC_SKIP_AI_CONFIDENCE = 0.85


def _model_cooled_down(model: str) -> bool:
    until = _MODEL_COOLDOWN.get(model)
    return until is not None and until > time.monotonic()


def _cool_down_model(model: str) -> None:
    _MODEL_COOLDOWN[model] = time.monotonic() + _MODEL_COOLDOWN_SECONDS


def _model_candidates(settings) -> list[str]:
    # Configured order is smartest-first; lite models stay last even if one
    # of them happened to answer last time (their answers are weaker).
    models: list[str] = []
    for value in (
        settings.gemini_model,
        settings.gemini_fallback_model,
        *settings.gemini_model_cascade.split(","),
    ):
        name = (value or "").strip()
        if name and name not in models:
            models.append(name)
    available = [m for m in models if not _model_cooled_down(m)]
    if len(available) < len(models):
        logger.info(
            "Skipping cooled-down Gemini models: %s",
            [m for m in models if _model_cooled_down(m)],
        )
    return available


async def _generate(
    client: genai.Client,
    model: str,
    contents: str,
    *,
    attempts: int = 1,
) -> ShoeRecommendation:
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                ),
            )
            content = response.text or "{}"
            return ShoeRecommendation.model_validate(json.loads(content))
        except Exception as exc:
            last_exc = exc
            if attempt < attempts and _is_retryable(exc):
                delay = 0.4 * attempt
                logger.warning(
                    "Gemini %s failed (attempt %s/%s): %s; retrying in %.1fs",
                    model,
                    attempt,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            raise
    assert last_exc is not None
    raise last_exc


def _apply_guards(
    recommendation: ShoeRecommendation,
    activity: dict[str, Any],
    shoes: list[dict[str, Any]],
    recent_activities: list[dict[str, Any]] | None = None,
    preferences: list[dict[str, Any]] | None = None,
    run_type_distances: list[dict[str, Any]] | None = None,
) -> ShoeRecommendation:
    preferences = preferences or []
    valid_ids = {s["id"] for s in shoes}
    if recommendation.shoe_id and recommendation.shoe_id not in valid_ids:
        logger.warning("AI returned invalid shoe_id %s; clearing", recommendation.shoe_id)
        recommendation.shoe_id = None
        recommendation.confidence = min(recommendation.confidence, 0.4)
        recommendation.reason += " (invalid shoe_id discarded)"

    # AI said unknown: try the user's regular distances, then similar runs.
    if recommendation.run_type == "unknown":
        distance_km = float(activity.get("distance") or 0) / 1000.0
        by_distance = classify_by_user_distance(distance_km or None, run_type_distances)
        if by_distance:
            recommendation.run_type = by_distance
            recommendation.reason += (
                f" (type inferred as {by_distance} from your usual distance)"
            )
        else:
            inferred = _infer_run_type_from_similar(
                activity, recent_activities or [], preferences
            )
            if inferred:
                recommendation.run_type = inferred
                recommendation.reason += (
                    f" (type inferred as {inferred} from similar recent runs)"
                )

    # Explicit /prefer pin wins for its run type.
    pin = _pinned_shoe(recommendation.run_type, preferences)
    if pin and pin.get("shoe_id") in valid_ids:
        pin_id = str(pin["shoe_id"])
        if recommendation.shoe_id != pin_id:
            recommendation.reason = (
                f"Using your /prefer pin for {recommendation.run_type}: "
                f"{pin.get('shoe_name') or pin_id}. AI note: {recommendation.reason}"
            )
            recommendation.shoe_id = pin_id
        recommendation.confidence = max(recommendation.confidence, 0.9)
        return recommendation

    current_gear = activity.get("gear_id")
    if (
        recommendation.shoe_id
        and current_gear
        and recommendation.shoe_id == current_gear
        and not _preference_supported(recommendation.run_type, recommendation.shoe_id, preferences)
    ):
        # Echoing current gear alone is weak evidence.
        recommendation.confidence = min(recommendation.confidence, 0.65)
        recommendation.reason += " (current gear alone is weak evidence)"
    return recommendation


async def recommend_shoe(
    activity: dict[str, Any],
    shoes: list[dict[str, Any]],
    recent_activities: list[dict[str, Any]],
    usage_trends: list[dict[str, Any]],
    preferences: list[dict[str, Any]] | None = None,
    run_type_distances: list[dict[str, Any]] | None = None,
) -> ShoeRecommendation:
    preferences = preferences or []
    settings = get_settings()
    if not shoes:
        return ShoeRecommendation(
            run_type="unknown",
            shoe_id=None,
            confidence=0.0,
            reason="No shoes available on the Strava athlete profile.",
        )

    if not settings.gemini_api_key:
        return _heuristic_from_history(
            activity,
            shoes,
            recent_activities,
            reason="Heuristic from last runs (GEMINI_API_KEY not set).",
            preferences=preferences,
            run_type_distances=run_type_distances,
        )

    # History/pin is already confident: don't pay for (or wait on) Gemini.
    pre_pick = _heuristic_from_history(
        activity,
        shoes,
        recent_activities,
        reason="History/pin pick (Gemini skipped).",
        preferences=preferences,
        run_type_distances=run_type_distances,
    )
    if pre_pick.shoe_id and pre_pick.confidence >= HEURISTIC_SKIP_AI_CONFIDENCE:
        logger.info(
            "Using history pick for activity (%s -> %s, %.0f%%); skipping Gemini",
            pre_pick.run_type,
            pre_pick.shoe_id,
            pre_pick.confidence * 100,
        )
        return pre_pick

    models = _model_candidates(settings)
    if not models:
        logger.warning("All Gemini models are in cooldown; using history heuristic")
        return _heuristic_from_history(
            activity,
            shoes,
            recent_activities,
            reason="Heuristic from last runs (Gemini models cooling down).",
            preferences=preferences,
            run_type_distances=run_type_distances,
        )

    client = genai.Client(api_key=settings.gemini_api_key)
    contents = _build_user_payload(
        activity, shoes, recent_activities, usage_trends, preferences, run_type_distances
    )

    global _LAST_GOOD_MODEL
    last_error: str | None = None
    best_weak: ShoeRecommendation | None = None
    for index, model in enumerate(models):
        try:
            recommendation = await _generate(client, model, contents)
            recommendation = _apply_guards(
                recommendation,
                activity,
                shoes,
                recent_activities,
                preferences,
                run_type_distances,
            )
            _LAST_GOOD_MODEL = model
            _MODEL_COOLDOWN.pop(model, None)
            if model != settings.gemini_model:
                recommendation.reason += f" [via {model}]"
            # Quality gate: a weak answer (unknown type / very low confidence)
            # from a lite fallback is not accepted while smarter models remain.
            is_weak = recommendation.run_type == "unknown" or recommendation.confidence < 0.5
            if is_weak and index < len(models) - 1:
                logger.info(
                    "Weak answer from %s (type=%s, %.0f%%); trying next model",
                    model,
                    recommendation.run_type,
                    recommendation.confidence * 100,
                )
                if best_weak is None or recommendation.confidence > best_weak.confidence:
                    best_weak = recommendation
                continue
            return recommendation
        except Exception as exc:
            last_error = str(exc)
            if _is_retryable(exc):
                _cool_down_model(model)
            logger.warning("Gemini model %s failed: %s", model, exc)

    if best_weak is not None:
        return best_weak

    logger.error("All Gemini models failed; using history heuristic: %s", last_error)
    return _heuristic_from_history(
        activity,
        shoes,
        recent_activities,
        reason=f"Heuristic from last runs (Gemini busy: {last_error})",
        preferences=preferences,
        run_type_distances=run_type_distances,
    )


def _heuristic_from_history(
    activity: dict[str, Any],
    shoes: list[dict[str, Any]],
    recent_activities: list[dict[str, Any]],
    reason: str = "Heuristic from last runs.",
    preferences: list[dict[str, Any]] | None = None,
    run_type_distances: list[dict[str, Any]] | None = None,
) -> ShoeRecommendation:
    preferences = preferences or []
    run_type = classify_heuristic(activity, run_type_distances)
    if run_type == "unknown":
        run_type = _infer_run_type_from_similar(activity, recent_activities, preferences) or run_type
    pin = _pinned_shoe(run_type, preferences)
    valid_ids = {s["id"] for s in shoes}
    if pin and pin.get("shoe_id") in valid_ids:
        return ShoeRecommendation(
            run_type=run_type,
            shoe_id=str(pin["shoe_id"]),
            confidence=0.9,
            reason=f"{reason} Using your /prefer pin for {run_type}.",
        )
    active_ids = {s["id"] for s in shoes if not s.get("retired")} or {s["id"] for s in shoes}

    # Prefer shoes used on recent runs of the same inferred type.
    same_type = [
        row["gear_id"]
        for row in recent_activities
        if row.get("inferred_run_type") == run_type and row.get("gear_id") in active_ids
    ]
    shoe_id = None
    if same_type:
        shoe_id, top_count = Counter(same_type).most_common(1)[0]
        if top_count >= 4:
            confidence = 0.85
        elif len(same_type) >= 2:
            confidence = 0.75
        else:
            confidence = 0.55
        reason = f"{reason} Most common shoe on recent {run_type} runs."
    else:
        any_geared = [row["gear_id"] for row in recent_activities if row.get("gear_id") in active_ids]
        if any_geared:
            shoe_id = Counter(any_geared).most_common(1)[0][0]
            confidence = 0.45
            reason = f"{reason} No same-type history; used most common recent shoe."
        else:
            active = [s for s in shoes if s["id"] in active_ids]
            if run_type in {"intervals", "race"}:
                active = sorted(active, key=lambda s: s.get("distance_meters", 0))
            shoe_id = active[0]["id"] if active else None
            confidence = 0.35
            reason = f"{reason} No geared history available."

    return ShoeRecommendation(
        run_type=run_type,
        shoe_id=shoe_id,
        confidence=confidence,
        reason=reason,
    )


# Back-compat alias used by older tests
def _heuristic_fallback(activity, shoes, preferences, reason="Heuristic fallback."):
    return _heuristic_from_history(activity, shoes, [], reason=reason)
