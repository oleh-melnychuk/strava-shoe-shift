from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.config import Settings, get_settings
from app.store import (
    clear_preferences,
    get_last_processed_for_user,
    list_preferences,
    upsert_telegram_user,
)
from app.strava.client import get_client_for_user, get_user_by_telegram_id
from app.strava.oauth import build_authorize_url
from app.ai.classifier import SETTABLE_RUN_TYPES
from app.telegram.handlers import prefer_callback, recalc_callback, shoe_callback

logger = logging.getLogger(__name__)

RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
RECALC_LIST_LIMIT = 10


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    await update.message.reply_text(
        "Strava Shoe Shift bot.\n\n"
        "After you upload a run, I classify it (long / tempo / intervals / recovery / race) "
        "and assign shoes from your Strava gear. If I'm unsure, I'll ask you.\n\n"
        "Commands:\n"
        "/link — connect Strava\n"
        "/shoes — list your shoes\n"
        "/recalc — recalculate shoes for a recent run\n"
        "/prefer — optional manual pin (history is primary)\n"
        "/prefs — show prefs · /prefs clear — wipe prefs\n"
        "/status — account status"
    )


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.effective_chat:
        return
    settings: Settings = context.application.bot_data["settings"]
    tg_user = update.effective_user
    await upsert_telegram_user(tg_user.id, update.effective_chat.id)
    url = build_authorize_url(settings, tg_user.id)
    await update.message.reply_text(f"Open this link to authorize Strava:\n{url}")


async def shoes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = await get_user_by_telegram_id(update.effective_user.id)
    if not user or not user.access_token:
        await update.message.reply_text("Link Strava first with /link")
        return
    try:
        client = await get_client_for_user(user)
        athlete = await client.get_athlete()
    except Exception:
        logger.exception("Failed to fetch shoes")
        await update.message.reply_text("Could not fetch shoes from Strava.")
        return

    shoes = athlete.get("shoes") or []
    if not shoes:
        await update.message.reply_text("No shoes found on your Strava profile.")
        return

    lines = []
    for shoe in shoes:
        km = round(float(shoe.get("distance") or 0) / 1000.0, 1)
        retired = " (retired)" if shoe.get("retired") else ""
        primary = " ★" if shoe.get("primary") else ""
        lines.append(f"• {shoe.get('name')}{primary}{retired} — {km} km")
    await update.message.reply_text("Your shoes:\n" + "\n".join(lines))


async def prefer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = await get_user_by_telegram_id(update.effective_user.id)
    if not user or not user.access_token:
        await update.message.reply_text("Link Strava first with /link")
        return

    args = context.args or []
    if args:
        run_type = args[0].strip().lower()
        if run_type not in SETTABLE_RUN_TYPES:
            await update.message.reply_text(
                "Unknown run type. Use one of: " + ", ".join(SETTABLE_RUN_TYPES)
            )
            return
        try:
            client = await get_client_for_user(user)
            athlete = await client.get_athlete()
        except Exception:
            logger.exception("Failed to fetch shoes for prefer")
            await update.message.reply_text("Could not fetch shoes from Strava.")
            return
        shoes = [s for s in (athlete.get("shoes") or []) if s.get("id") and not s.get("retired")]
        if not shoes:
            shoes = [s for s in (athlete.get("shoes") or []) if s.get("id")]
        if not shoes:
            await update.message.reply_text("No shoes found on your Strava profile.")
            return
        shoe_buttons = [
            [
                InlineKeyboardButton(
                    text=(s.get("name") or s["id"])[:64],
                    callback_data=f"prefer:set:{run_type}:{s['id']}",
                )
            ]
            for s in shoes[:10]
        ]
        await update.message.reply_text(
            f"Pick your preferred shoe for {run_type} runs:",
            reply_markup=InlineKeyboardMarkup(shoe_buttons),
        )
        return

    buttons = [
        [InlineKeyboardButton(text=rt, callback_data=f"prefer:type:{rt}")]
        for rt in SETTABLE_RUN_TYPES
    ]
    await update.message.reply_text(
        "Which run type do you want to set a preferred shoe for?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def prefs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = await get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("Link Strava first with /link")
        return

    args = [a.lower() for a in (context.args or [])]
    if args and args[0] == "clear":
        removed = await clear_preferences(user.id)
        await update.message.reply_text(
            f"Cleared {removed} preference(s). "
            "Shoe picks now use your last 20 Strava runs only."
        )
        return

    rows = await list_preferences(user.id)
    if not rows:
        await update.message.reply_text(
            "No stored preferences.\n"
            "Assignments use analysis of your last 20 Strava runs."
        )
        return

    lines = [
        "Stored prefs (not used for assignment — history is primary):",
    ]
    for row in sorted(rows, key=lambda r: (r.run_type, -r.accept_count)):
        name = row.shoe_name or row.shoe_id
        lines.append(f"• {row.run_type} → {name} (✓{row.accept_count} / ✗{row.reject_count})")
    lines.append("\nSend /prefs clear to wipe these.")
    await update.message.reply_text("\n".join(lines))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = await get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("Not registered. Use /start then /link.")
        return

    linked = bool(user.strava_athlete_id and user.access_token)
    last_act = await get_last_processed_for_user(user.id)

    lines = [
        f"Telegram ID: {user.telegram_user_id}",
        f"Strava linked: {'yes' if linked else 'no'}",
        f"Strava athlete ID: {user.strava_athlete_id or '—'}",
        f"Skip if already geared: {user.skip_if_geared}",
    ]
    if last_act:
        lines.append(
            f"Last assignment: activity {last_act.strava_activity_id} "
            f"[{last_act.status}] type={last_act.run_type} shoe={last_act.shoe_id}"
        )
    await update.message.reply_text("\n".join(lines))


def _activity_button_label(activity: dict) -> str:
    name = (activity.get("name") or "Run").strip() or "Run"
    km = round(float(activity.get("distance") or 0) / 1000.0, 1)
    start = activity.get("start_date_local") or activity.get("start_date") or ""
    day = ""
    if start:
        try:
            day = datetime.fromisoformat(start.replace("Z", "+00:00")).strftime("%m/%d")
        except ValueError:
            day = start[:10]
    gear = "👟" if activity.get("gear_id") else "—"
    label = f"{day} {name} · {km}km {gear}".strip()
    return label[:64]


async def recalc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = await get_user_by_telegram_id(update.effective_user.id)
    if not user or not user.access_token:
        await update.message.reply_text("Link Strava first with /link")
        return

    try:
        client = await get_client_for_user(user)
        activities = await client.list_activities(page=1, per_page=30)
    except Exception:
        logger.exception("Failed to fetch activities for recalc")
        await update.message.reply_text("Could not fetch activities from Strava.")
        return

    runs = [
        a
        for a in activities
        if (a.get("type") or a.get("sport_type")) in RUN_TYPES
    ][:RECALC_LIST_LIMIT]

    if not runs:
        await update.message.reply_text("No recent runs found.")
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=_activity_button_label(act),
                callback_data=f"recalc:{act['id']}",
            )
        ]
        for act in runs
        if act.get("id")
    ]
    await update.message.reply_text(
        "Pick a run to recalculate shoes:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def build_application(settings: Settings | None = None) -> Application:
    settings = settings or get_settings()
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.bot_data["settings"] = settings
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("link", link_command))
    app.add_handler(CommandHandler("shoes", shoes_command))
    app.add_handler(CommandHandler("recalc", recalc_command))
    app.add_handler(CommandHandler("prefer", prefer_command))
    app.add_handler(CommandHandler("prefs", prefs_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CallbackQueryHandler(shoe_callback, pattern=r"^shoe:"))
    app.add_handler(CallbackQueryHandler(recalc_callback, pattern=r"^recalc:"))
    app.add_handler(CallbackQueryHandler(prefer_callback, pattern=r"^prefer:"))
    return app
