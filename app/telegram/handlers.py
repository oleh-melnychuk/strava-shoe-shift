from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.ai.classifier import SETTABLE_RUN_TYPES, normalize_run_type
from app.models import User
from app.services.assign_shoe import apply_shoe_choice, process_activity_for_user
from app.store import (
    get_pending,
    get_processed,
    get_user_by_telegram_id,
    save_pending,
    save_processed,
    set_explicit_preference,
)
from app.strava.client import get_client_for_user

logger = logging.getLogger(__name__)


async def prefer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callbacks: prefer:type:<run_type> or prefer:set:<run_type>:<shoe_id>."""
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) < 3 or parts[0] != "prefer":
        await query.edit_message_text("Invalid preference choice.")
        return

    user = await get_user_by_telegram_id(query.from_user.id)
    if not user or not user.access_token:
        await query.edit_message_text("Link Strava first with /link")
        return

    action = parts[1]
    if action == "type" and len(parts) == 3:
        run_type = normalize_run_type(parts[2])
        if run_type not in SETTABLE_RUN_TYPES:
            await query.edit_message_text("Unknown run type.")
            return
        try:
            client = await get_client_for_user(user)
            athlete = await client.get_athlete()
        except Exception:
            logger.exception("Failed to fetch shoes for prefer")
            await query.edit_message_text("Could not fetch shoes from Strava.")
            return

        shoes = [s for s in (athlete.get("shoes") or []) if s.get("id") and not s.get("retired")]
        if not shoes:
            shoes = [s for s in (athlete.get("shoes") or []) if s.get("id")]
        if not shoes:
            await query.edit_message_text("No shoes found on your Strava profile.")
            return

        buttons = [
            [
                InlineKeyboardButton(
                    text=(s.get("name") or s["id"])[:64],
                    callback_data=f"prefer:set:{run_type}:{s['id']}",
                )
            ]
            for s in shoes[:10]
        ]
        await query.edit_message_text(
            f"Pick your preferred shoe for <b>{html.escape(run_type)}</b> runs:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if action == "set" and len(parts) == 4:
        run_type = normalize_run_type(parts[2])
        shoe_id = parts[3]
        if run_type not in SETTABLE_RUN_TYPES:
            await query.edit_message_text("Unknown run type.")
            return
        shoe_name = shoe_id
        try:
            client = await get_client_for_user(user)
            athlete = await client.get_athlete()
            for shoe in athlete.get("shoes") or []:
                if shoe.get("id") == shoe_id:
                    shoe_name = shoe.get("name") or shoe_id
                    break
        except Exception:
            logger.exception("Failed to resolve shoe name for prefer")

        await set_explicit_preference(user.id, run_type, shoe_id, shoe_name)
        await query.edit_message_text(
            f"Saved: <b>{html.escape(run_type)}</b> → <b>{html.escape(shoe_name)}</b>.\n"
            "I'll prefer this shoe next time.",
            parse_mode="HTML",
        )
        return

    await query.edit_message_text("Invalid preference choice.")


async def recalc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 2 or parts[0] != "recalc":
        await query.edit_message_text("Invalid choice.")
        return

    try:
        activity_id = int(parts[1])
    except ValueError:
        await query.edit_message_text("Invalid activity id.")
        return

    user = await get_user_by_telegram_id(query.from_user.id)
    if not user or not user.access_token:
        await query.edit_message_text("Link Strava first with /link")
        return

    await query.edit_message_text(f"Recalculating shoes for activity {activity_id}…")
    try:
        await process_activity_for_user(user, activity_id, force=True)
    except Exception:
        logger.exception("Failed to recalculate activity %s", activity_id)
        if query.message:
            await query.message.reply_text("Failed to recalculate. Try again later.")


async def shoe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3 or parts[0] != "shoe":
        await query.edit_message_text("Invalid choice.")
        return

    pending_id = int(parts[1])
    choice = parts[2]

    pending = await get_pending(pending_id)
    if not pending or pending.resolved:
        await query.edit_message_text("This decision is no longer available.")
        return

    expires_at = datetime.fromisoformat(pending.expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        pending.resolved = True
        await save_pending(pending)
        await query.edit_message_text("This choice expired.")
        return

    user = await get_user_by_telegram_id(pending.user_id)
    if not user or user.telegram_user_id != query.from_user.id:
        await query.edit_message_text("Unauthorized.")
        return

    if choice == "skip":
        pending.resolved = True
        await save_pending(pending)
        processed = await get_processed(pending.strava_activity_id)
        if processed:
            processed.status = "skipped_by_user"
            await save_processed(processed)
        await query.edit_message_text("Skipped shoe assignment.")
        return

    shoe_id = choice
    shoe_name = shoe_id
    try:
        for opt in json.loads(pending.options_json):
            if opt.get("id") == shoe_id:
                shoe_name = opt.get("name") or shoe_id
                break
    except Exception:
        pass

    try:
        await apply_shoe_choice(
            user,
            pending.strava_activity_id,
            shoe_id,
            pending.run_type,
            shoe_name=shoe_name,
            notify=True,
            reason=f"Confirmed by you for a {pending.run_type} run.",
        )
    except Exception:
        logger.exception("Failed to apply shoe choice")
        await query.edit_message_text("Failed to update Strava gear. Try again later.")
        return

    pending.resolved = True
    await save_pending(pending)
    await query.edit_message_text(
        f"Set shoes to <b>{html.escape(shoe_name)}</b> for your "
        f"{html.escape(pending.run_type)} run.",
        parse_mode="HTML",
    )


async def resolve_link_notify(user: User, athlete_name: str, bot_token: str) -> None:
    from app.telegram.onboarding import start_onboarding_after_link

    await start_onboarding_after_link(user, athlete_name, bot_token)
