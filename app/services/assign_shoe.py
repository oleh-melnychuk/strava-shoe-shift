from __future__ import annotations

import html
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from app.ai.classifier import classify_heuristic, normalize_run_type
from app.ai.recommender import build_recent_activity_context, recommend_shoe
from app.config import get_settings
from app.models import ProcessedActivity, User
from app.store import (
    clear_processed,
    create_pending,
    get_processed,
    list_preferences,
    list_run_distances,
    save_processed,
)
from app.strava.client import get_client_for_user, get_user_by_strava_athlete_id

RECENT_HISTORY_LIMIT = 50

logger = logging.getLogger(__name__)


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


async def _notify(bot: Bot, chat_id: int, text: str, **kwargs: Any) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", **kwargs)
    except Exception:
        logger.exception("Failed to send Telegram notification")


def _assignment_message(
    activity_id: int,
    activity_name: str | None,
    run_type: str,
    shoe_name: str | None,
    *,
    shoe_id: str | None = None,
    previous_shoe_name: str | None = None,
    previous_shoe_id: str | None = None,
    confidence: float | None = None,
    reason: str | None = None,
    prefix: str = "",
) -> str:
    name = activity_name or f"Activity {activity_id}"
    changed = bool(previous_shoe_id and shoe_id and str(previous_shoe_id) != str(shoe_id))
    same = bool(previous_shoe_id and shoe_id and str(previous_shoe_id) == str(shoe_id))
    title = "Shoes changed" if changed else ("Shoes confirmed" if same else "Shoes assigned")
    lines = [
        f"{_esc(prefix)}👟 <b>{title}</b>",
        f"<b>{_esc(name)}</b> — {_esc(run_type)} run",
    ]
    if changed:
        lines.append(f"{_esc(previous_shoe_name or previous_shoe_id)} → <b>{_esc(shoe_name or shoe_id)}</b>")
    else:
        lines.append(f"<b>{_esc(shoe_name or shoe_id)}</b>")
    if confidence is not None:
        lines.append(f"Confidence: {confidence:.0%}")
    if reason:
        lines.append(_esc(reason))
    lines.append(f"https://www.strava.com/activities/{activity_id}")
    return "\n".join(lines)


async def notify_shoe_assigned(
    user: User,
    activity_id: int,
    shoe_id: str,
    shoe_name: str | None,
    run_type: str,
    *,
    activity_name: str | None = None,
    previous_shoe_name: str | None = None,
    previous_shoe_id: str | None = None,
    confidence: float | None = None,
    reason: str | None = None,
    prefix: str = "",
    bot: Bot | None = None,
) -> None:
    settings = get_settings()
    bot = bot or Bot(token=settings.telegram_bot_token)
    await _notify(
        bot,
        user.telegram_chat_id,
        _assignment_message(
            activity_id,
            activity_name,
            run_type,
            shoe_name,
            shoe_id=shoe_id,
            previous_shoe_name=previous_shoe_name,
            previous_shoe_id=previous_shoe_id,
            confidence=confidence,
            reason=reason,
            prefix=prefix,
        ),
    )


def _meters_to_km(meters: float | int | None) -> float:
    return round(float(meters or 0) / 1000.0, 1)


def _primary_shoe_id(shoes: list[dict[str, Any]]) -> str | None:
    for shoe in shoes:
        if shoe.get("primary") and shoe.get("id"):
            return str(shoe["id"])
    return None


def _is_strava_default_gear(gear_id: str | None, primary_shoe_id: str | None) -> bool:
    """Strava auto-tags uploads with the athlete's primary (default) shoe."""
    return bool(gear_id and primary_shoe_id and str(gear_id) == primary_shoe_id)


def _should_skip_geared(
    gear_id: str | None,
    primary_shoe_id: str | None,
    skip_if_geared: bool,
    *,
    force: bool = False,
) -> bool:
    if force or not gear_id or not skip_if_geared:
        return False
    # Default gear is not a deliberate choice — let the bot reassign it.
    return not _is_strava_default_gear(gear_id, primary_shoe_id)


def _shoe_payload(shoes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": s.get("id"),
            "name": s.get("name") or s.get("nickname") or s.get("id"),
            "distance_meters": s.get("distance", 0),
            "distance_km": _meters_to_km(s.get("distance")),
            "retired": bool(s.get("retired")),
            "primary": bool(s.get("primary")),
        }
        for s in shoes
        if s.get("id")
    ]


def _usage_trends(
    activities: list[dict[str, Any]],
    shoe_names: dict[str, str],
    run_type_distances: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "distance_m": 0.0,
            "recent_names": [],
            "by_run_type": defaultdict(int),
        }
    )
    for act in activities:
        gear_id = act.get("gear_id")
        if not gear_id:
            continue
        if (act.get("type") or act.get("sport_type")) not in {"Run", "TrailRun", "VirtualRun"}:
            continue
        bucket = stats[gear_id]
        bucket["count"] += 1
        bucket["distance_m"] += float(act.get("distance") or 0)
        run_type = classify_heuristic(act, run_type_distances)
        bucket["by_run_type"][run_type] += 1
        if len(bucket["recent_names"]) < 5:
            bucket["recent_names"].append(act.get("name"))

    trends = []
    for gear_id, data in stats.items():
        trends.append(
            {
                "shoe_id": gear_id,
                "shoe_name": shoe_names.get(gear_id, gear_id),
                "run_count": data["count"],
                "distance_km": _meters_to_km(data["distance_m"]),
                "by_run_type": dict(data["by_run_type"]),
                "recent_activity_names": data["recent_names"],
            }
        )
    trends.sort(key=lambda t: t["run_count"], reverse=True)
    return trends


async def _load_preferences(user_id: int) -> list[dict[str, Any]]:
    rows = await list_preferences(user_id)
    return [
        {
            "run_type": r.run_type,
            "shoe_id": r.shoe_id,
            "shoe_name": r.shoe_name,
            "accept_count": r.accept_count,
            "reject_count": r.reject_count,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


async def _load_run_distances(user_id: int) -> list[dict[str, Any]]:
    rows = await list_run_distances(user_id)
    return [
        {
            "run_type": r.run_type,
            "min_km": r.min_km,
            "max_km": r.max_km,
            "typical_km": r.typical_km,
        }
        for r in rows
    ]


async def _mark_processed(
    user_id: int,
    activity_id: int,
    run_type: str | None,
    shoe_id: str | None,
    confidence: float | None,
    status: str,
) -> None:
    existing = await get_processed(activity_id)
    if existing:
        existing.run_type = run_type or existing.run_type
        existing.shoe_id = shoe_id or existing.shoe_id
        if confidence is not None:
            existing.confidence = confidence
        existing.status = status
        await save_processed(existing)
        return

    await save_processed(
        ProcessedActivity(
            strava_activity_id=activity_id,
            user_id=user_id,
            run_type=run_type,
            shoe_id=shoe_id,
            confidence=confidence,
            status=status,
        )
    )


async def apply_shoe_choice(
    user: User,
    activity_id: int,
    shoe_id: str,
    run_type: str,
    shoe_name: str | None = None,
    confidence: float | None = None,
    *,
    notify: bool = False,
    activity_name: str | None = None,
    previous_shoe_id: str | None = None,
    previous_shoe_name: str | None = None,
    reason: str | None = None,
    prefix: str = "",
    bot: Bot | None = None,
) -> dict[str, Any]:
    client = await get_client_for_user(user)
    if notify and (activity_name is None or previous_shoe_id is None):
        try:
            current = await client.get_activity(activity_id)
            activity_name = activity_name or current.get("name")
            previous_shoe_id = previous_shoe_id or current.get("gear_id")
        except Exception:
            logger.exception("Could not load activity %s before assignment notify", activity_id)
    updated = await client.update_activity_gear(activity_id, shoe_id)
    await _mark_processed(
        user.id,
        activity_id,
        run_type,
        shoe_id,
        confidence,
        status="assigned",
    )
    if notify:
        await notify_shoe_assigned(
            user,
            activity_id,
            shoe_id,
            shoe_name,
            run_type,
            activity_name=activity_name,
            previous_shoe_name=previous_shoe_name,
            previous_shoe_id=previous_shoe_id,
            confidence=confidence,
            reason=reason,
            prefix=prefix,
            bot=bot,
        )
    return updated


async def process_activity_for_user(user: User, activity_id: int, *, force: bool = False) -> None:
    """Classify and assign shoes for an activity. force=True re-runs even if already processed/geared."""
    settings = get_settings()

    if force:
        await clear_processed(activity_id, user.id)
    elif await get_processed(activity_id):
        logger.info("Activity %s already processed", activity_id)
        return

    logger.info(
        "Processing activity %s for telegram user %s (force=%s)",
        activity_id,
        user.telegram_user_id,
        force,
    )
    client = await get_client_for_user(user)
    activity = await client.get_activity(activity_id)
    sport = activity.get("type") or activity.get("sport_type")
    if sport not in {"Run", "TrailRun", "VirtualRun"}:
        logger.info("Activity %s ignored: not a run (type=%s)", activity_id, sport)
        await _mark_processed(user.id, activity_id, None, None, None, "ignored_non_run")
        return

    athlete = await client.get_athlete()
    shoes_raw = athlete.get("shoes") or []
    shoes = _shoe_payload(shoes_raw)
    shoe_names = {s["id"]: s["name"] for s in shoes}
    primary_shoe_id = _primary_shoe_id(shoes)
    gear_id = activity.get("gear_id")

    if _is_strava_default_gear(gear_id, primary_shoe_id):
        logger.info(
            "Activity %s has Strava default gear %s (%s); will reassign",
            activity_id,
            gear_id,
            shoe_names.get(str(gear_id), gear_id),
        )
    elif _should_skip_geared(gear_id, primary_shoe_id, user.skip_if_geared, force=force):
        logger.info(
            "Activity %s skipped: already geared %s (%s) and skip_if_geared=true",
            activity_id,
            gear_id,
            shoe_names.get(str(gear_id), gear_id),
        )
        await _mark_processed(
            user.id,
            activity_id,
            None,
            gear_id,
            None,
            "skipped_already_geared",
        )
        return
    elif gear_id:
        logger.info(
            "Activity %s already geared %s; proceeding (force=%s, skip_if_geared=%s)",
            activity_id,
            gear_id,
            force,
            user.skip_if_geared,
        )

    # Pull enough activities to get ~50 runs after filtering non-runs.
    recent_raw = await client.list_activities(page=1, per_page=100)
    # Exclude the activity we are assigning from history so we don't echo its gear.
    recent_raw = [a for a in recent_raw if a.get("id") != activity_id]
    recent_runs = [
        a
        for a in recent_raw
        if (a.get("type") or a.get("sport_type")) in {"Run", "TrailRun", "VirtualRun"}
    ][:RECENT_HISTORY_LIMIT]
    preferences = await _load_preferences(user.id)
    run_type_distances = await _load_run_distances(user.id)
    recent_activities = build_recent_activity_context(
        recent_runs,
        shoe_names,
        limit=RECENT_HISTORY_LIMIT,
        run_type_distances=run_type_distances,
    )
    trends = _usage_trends(recent_runs, shoe_names, run_type_distances)

    recommendation = await recommend_shoe(
        activity, shoes, recent_activities, trends, preferences, run_type_distances
    )
    run_type = normalize_run_type(recommendation.run_type)
    shoe_id = recommendation.shoe_id
    confidence = float(recommendation.confidence)
    reason = recommendation.reason
    shoe_name = shoe_names.get(shoe_id) if shoe_id else None

    bot = Bot(token=settings.telegram_bot_token)
    activity_name = activity.get("name") or f"Activity {activity_id}"
    prefix = "Recalculated: " if force else ""

    if shoe_id and confidence >= settings.confidence_threshold:
        await apply_shoe_choice(
            user,
            activity_id,
            shoe_id,
            run_type,
            shoe_name=shoe_name,
            confidence=confidence,
            notify=True,
            activity_name=activity_name,
            previous_shoe_id=gear_id,
            previous_shoe_name=shoe_names.get(str(gear_id)) if gear_id else None,
            reason=reason,
            prefix=prefix,
            bot=bot,
        )
        return

    options = shoes[:6]
    if shoe_id:
        options = [s for s in shoes if s["id"] == shoe_id] + [s for s in shoes if s["id"] != shoe_id]
        options = options[:6]

    pending = await create_pending(
        user_id=user.id,
        strava_activity_id=activity_id,
        run_type=run_type,
        options_json=json.dumps(options),
        recommended_shoe_id=shoe_id,
        reason=reason,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )

    buttons: list[list[InlineKeyboardButton]] = []
    for shoe in options:
        label = shoe["name"]
        if shoe_id and shoe["id"] == shoe_id:
            label = f"★ {label}"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"shoe:{pending.id}:{shoe['id']}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="Skip", callback_data=f"shoe:{pending.id}:skip")])

    conf_txt = f"{confidence:.0%}" if shoe_id else "n/a"
    await _notify(
        bot,
        user.telegram_chat_id,
        (
            f"{_esc(prefix)}Not sure which shoes for \"{_esc(activity_name)}\" "
            f"(guessed <b>{_esc(run_type)}</b>, confidence {_esc(conf_txt)}).\n"
            f"{_esc(reason)}\n\nPick a shoe:"
        ),
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    await _mark_processed(
        user.id,
        activity_id,
        run_type,
        shoe_id,
        confidence,
        status="awaiting_confirmation",
    )


async def handle_activity_created(activity_id: int, athlete_id: int, *, force: bool = False) -> None:
    user = await get_user_by_strava_athlete_id(athlete_id)
    if not user or not user.access_token:
        logger.info("No linked user for athlete %s", athlete_id)
        return
    await process_activity_for_user(user, activity_id, force=force)
