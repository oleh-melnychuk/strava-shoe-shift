from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from app.ai.classifier import classify_heuristic
from app.config import get_settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You assign running shoes for Strava activities.

Classify the run type as one of: long, tempo, intervals, recovery, race, unknown.
Then pick the best shoe_id from the provided shoe list.

Primary evidence (use this, not stored preferences):
- recent_activities: the athlete's last ~20 runs with name, distance, pace, HR, gear, and
  an inferred_run_type hint. Find which shoes they actually used for similar efforts.
- usage_trends: aggregates from those recent runs (by_run_type counts per shoe).

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
    limit: int = 20,
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
                "inferred_run_type": classify_heuristic(act),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _build_user_payload(
    activity: dict[str, Any],
    shoes: list[dict[str, Any]],
    recent_activities: list[dict[str, Any]],
    usage_trends: list[dict[str, Any]],
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
        "recent_activities": recent_activities,
        "usage_trends": usage_trends,
        "instruction": (
            "Ignore stored prefs. Classify this activity, find similar rows in "
            "recent_activities, and pick the shoe those similar runs used most."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.ServerError):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return status in {429, 500, 502, 503, 504}


def _model_candidates(settings) -> list[str]:
    models: list[str] = []
    for value in (
        settings.gemini_model,
        settings.gemini_fallback_model,
        *settings.gemini_model_cascade.split(","),
    ):
        name = (value or "").strip()
        if name and name not in models:
            models.append(name)
    return models


async def _generate(
    client: genai.Client,
    model: str,
    contents: str,
    *,
    attempts: int = 2,
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
) -> ShoeRecommendation:
    valid_ids = {s["id"] for s in shoes}
    if recommendation.shoe_id and recommendation.shoe_id not in valid_ids:
        logger.warning("AI returned invalid shoe_id %s; clearing", recommendation.shoe_id)
        recommendation.shoe_id = None
        recommendation.confidence = min(recommendation.confidence, 0.4)
        recommendation.reason += " (invalid shoe_id discarded)"

    current_gear = activity.get("gear_id")
    if recommendation.shoe_id and current_gear and recommendation.shoe_id == current_gear:
        # Echoing current gear alone is weak evidence.
        recommendation.confidence = min(recommendation.confidence, 0.65)
        recommendation.reason += " (current gear alone is weak evidence)"
    return recommendation


async def recommend_shoe(
    activity: dict[str, Any],
    shoes: list[dict[str, Any]],
    recent_activities: list[dict[str, Any]],
    usage_trends: list[dict[str, Any]],
) -> ShoeRecommendation:
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
        )

    client = genai.Client(api_key=settings.gemini_api_key)
    contents = _build_user_payload(activity, shoes, recent_activities, usage_trends)
    models = _model_candidates(settings)

    last_error: str | None = None
    for model in models:
        try:
            recommendation = await _generate(client, model, contents)
            recommendation = _apply_guards(recommendation, activity, shoes)
            if model != settings.gemini_model:
                recommendation.reason += f" [via {model}]"
            return recommendation
        except Exception as exc:
            last_error = str(exc)
            logger.warning("Gemini model %s failed: %s", model, exc)

    logger.error("All Gemini models failed; using history heuristic: %s", last_error)
    return _heuristic_from_history(
        activity,
        shoes,
        recent_activities,
        reason=f"Heuristic from last runs (Gemini busy: {last_error})",
    )


def _heuristic_from_history(
    activity: dict[str, Any],
    shoes: list[dict[str, Any]],
    recent_activities: list[dict[str, Any]],
    reason: str = "Heuristic from last runs.",
) -> ShoeRecommendation:
    run_type = classify_heuristic(activity)
    active_ids = {s["id"] for s in shoes if not s.get("retired")} or {s["id"] for s in shoes}

    # Prefer shoes used on recent runs of the same inferred type.
    same_type = [
        row["gear_id"]
        for row in recent_activities
        if row.get("inferred_run_type") == run_type and row.get("gear_id") in active_ids
    ]
    shoe_id = None
    if same_type:
        shoe_id = Counter(same_type).most_common(1)[0][0]
        confidence = 0.75 if len(same_type) >= 2 else 0.55
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
