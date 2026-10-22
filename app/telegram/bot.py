from __future__ import annotations

import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.ai.classifier import SETTABLE_RUN_TYPES
from app.config import Settings, get_settings
from app.store import (
    clear_preferences,
    get_last_processed_for_user,
    list_preferences,
    upsert_telegram_user,
)
from app.strava.client import get_client_for_user, get_user_by_telegram_id
from app.strava.oauth import build_authorize_url
from app.telegram.handlers import prefer_callback, recalc_callback, shoe_callback
from app.telegram.keyboards import (
    BTN_LINK,
    BTN_PREFER,
    BTN_PREFS,
    BTN_RECALC,
    BTN_SHOES,
    BTN_STATUS,
    MAIN_BUTTONS,
    home_inline_keyboard,
    main_menu_keyboard,
    prefs_actions_keyboard,
)

logger = logging.getLogger(__name__)

RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
RECALC_LIST_LIMIT = 10


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    await update.message.reply_text(
        "Strava Shoe Shift\n\n"
        "I watch new runs, classify them from your last 50 activities, "
        "and assign shoes. If I'm unsure, I'll ask.\n\n"
        "Use the buttons below:",
        reply_markup=main_menu_keyboard(),
    )
    await update.message.reply_text(
        "Shortcuts:",
        reply_markup=home_inline_keyboard(),
    )


async def link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.effective_chat:
        return
    settings: Settings = context.application.bot_data["settings"]
    tg_user = update.effective_user
    await upsert_telegram_user(tg_user.id, update.effective_chat.id)
    url = build_authorize_url(settings, tg_user.id)
    await update.message.reply_text(
        f"Open this link to authorize Strava:\n{url}",
        reply_markup=main_menu_keyboard(),
    )


async def shoes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = await get_user_by_telegram_id(update.effective_user.id)
    if not user or not user.access_token:
        await update.message.reply_text(
            "Link Strava first — tap Link Strava.",
            reply_markup=main_menu_keyboard(),
        )
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
    await update.message.reply_text(
        "Your shoes:\n" + "\n".join(lines),
        reply_markup=main_menu_keyboard(),
    )


async def prefer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = await get_user_by_telegram_id(update.effective_user.id)
    if not user or not user.access_token:
        await update.message.reply_text(
            "Link Strava first — tap Link Strava.",
            reply_markup=main_menu_keyboard(),
        )
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
        "Which run type do you want to set a preferred shoe for?\n"
        "(Optional — assignments mainly use your last 50 runs.)",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def prefs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = await get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text(
            "Not registered yet — tap Menu / start the bot.",
            reply_markup=main_menu_keyboard(),
        )
        return

    args = [a.lower() for a in (context.args or [])]
    if args and args[0] == "clear":
        removed = await clear_preferences(user.id)
        await update.message.reply_text(
            f"Cleared {removed} preference(s).\n"
            "Shoe picks use your last 50 Strava runs.",
            reply_markup=main_menu_keyboard(),
        )
        return

    rows = await list_preferences(user.id)
    if not rows:
        await update.message.reply_text(
            "No stored preferences.\n"
            "Assignments use your /prefer pins, then your last 50 Strava runs.",
            reply_markup=main_menu_keyboard(),
        )
        return

    lines = [
        "Pinned via /prefer (wins for its run type):",
    ]
    for row in sorted(rows, key=lambda r: (r.run_type, -r.accept_count)):
        name = row.shoe_name or row.shoe_id
        lines.append(f"• {row.run_type} → {name} (✓{row.accept_count} / ✗{row.reject_count})")
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=prefs_actions_keyboard(),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = await get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text(
            "Not registered. Tap Menu or send /start.",
            reply_markup=main_menu_keyboard(),
        )
        return

    linked = bool(user.strava_athlete_id and user.access_token)
    last_act = await get_last_processed_for_user(user.id)

    lines = [
        f"Telegram ID: {user.telegram_user_id}",
        f"Strava linked: {'yes' if linked else 'no'}",
        f"Strava athlete ID: {user.strava_athlete_id or '—'}",
        f"Skip manually-geared (non-default): {user.skip_if_geared}",
    ]
    if last_act:
        lines.append(
            f"Last assignment: activity {last_act.strava_activity_id} "
            f"[{last_act.status}] type={last_act.run_type} shoe={last_act.shoe_id}"
        )
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=main_menu_keyboard(),
    )


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
        await update.message.reply_text(
            "Link Strava first — tap Link Strava.",
            reply_markup=main_menu_keyboard(),
        )
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


async def menu_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route persistent reply-keyboard button presses."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if text not in MAIN_BUTTONS:
        await update.message.reply_text(
            "Use the menu buttons below.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if text == BTN_LINK:
        await link_command(update, context)
    elif text == BTN_SHOES:
        await shoes_command(update, context)
    elif text == BTN_RECALC:
        await recalc_command(update, context)
    elif text == BTN_PREFER:
        await prefer_command(update, context)
    elif text == BTN_PREFS:
        await prefs_command(update, context)
    elif text == BTN_STATUS:
        await status_command(update, context)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline home-menu and prefs clear actions."""
    query = update.callback_query
    if not query or not query.data or not query.from_user or not query.message:
        return
    await query.answer()

    action = query.data.split(":", 1)[-1] if query.data.startswith("menu:") else ""
    tg_user_id = query.from_user.id
    chat_id = query.message.chat_id
    message = query.message

    if action == "prefs_clear":
        user = await get_user_by_telegram_id(tg_user_id)
        if not user:
            await query.edit_message_text("Not registered. Send /start.")
            return
        removed = await clear_preferences(user.id)
        await query.edit_message_text(
            f"Cleared {removed} preference(s).\n"
            "Shoe picks use your last 50 Strava runs."
        )
        return

    if action == "link":
        settings: Settings = context.application.bot_data["settings"]
        await upsert_telegram_user(tg_user_id, chat_id)
        url = build_authorize_url(settings, tg_user_id)
        await message.reply_text(
            f"Open this link to authorize Strava:\n{url}",
            reply_markup=main_menu_keyboard(),
        )
        return

    if action == "shoes":
        user = await get_user_by_telegram_id(tg_user_id)
        if not user or not user.access_token:
            await message.reply_text(
                "Link Strava first — tap Link Strava.",
                reply_markup=main_menu_keyboard(),
            )
            return
        try:
            client = await get_client_for_user(user)
            athlete = await client.get_athlete()
        except Exception:
            logger.exception("Failed to fetch shoes")
            await message.reply_text("Could not fetch shoes from Strava.")
            return
        shoes = athlete.get("shoes") or []
        if not shoes:
            await message.reply_text("No shoes found on your Strava profile.")
            return
        lines = []
        for shoe in shoes:
            km = round(float(shoe.get("distance") or 0) / 1000.0, 1)
            retired = " (retired)" if shoe.get("retired") else ""
            primary = " ★" if shoe.get("primary") else ""
            lines.append(f"• {shoe.get('name')}{primary}{retired} — {km} km")
        await message.reply_text(
            "Your shoes:\n" + "\n".join(lines),
            reply_markup=main_menu_keyboard(),
        )
        return

    if action == "recalc":
        user = await get_user_by_telegram_id(tg_user_id)
        if not user or not user.access_token:
            await message.reply_text(
                "Link Strava first — tap Link Strava.",
                reply_markup=main_menu_keyboard(),
            )
            return
        try:
            client = await get_client_for_user(user)
            activities = await client.list_activities(page=1, per_page=30)
        except Exception:
            logger.exception("Failed to fetch activities for recalc")
            await message.reply_text("Could not fetch activities from Strava.")
            return
        runs = [
            a
            for a in activities
            if (a.get("type") or a.get("sport_type")) in RUN_TYPES
        ][:RECALC_LIST_LIMIT]
        if not runs:
            await message.reply_text("No recent runs found.")
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
        await message.reply_text(
            "Pick a run to recalculate shoes:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if action == "prefer":
        buttons = [
            [InlineKeyboardButton(text=rt, callback_data=f"prefer:type:{rt}")]
            for rt in SETTABLE_RUN_TYPES
        ]
        await message.reply_text(
            "Which run type do you want to set a preferred shoe for?\n"
            "(Optional — assignments mainly use your last 50 runs.)",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if action == "prefs":
        user = await get_user_by_telegram_id(tg_user_id)
        if not user:
            await message.reply_text(
                "Not registered yet — send /start.",
                reply_markup=main_menu_keyboard(),
            )
            return
        rows = await list_preferences(user.id)
        if not rows:
            await message.reply_text(
                "No stored preferences.\n"
                "Assignments use your /prefer pins, then your last 50 Strava runs.",
                reply_markup=main_menu_keyboard(),
            )
            return
        lines = ["Pinned via /prefer (wins for its run type):"]
        for row in sorted(rows, key=lambda r: (r.run_type, -r.accept_count)):
            name = row.shoe_name or row.shoe_id
            lines.append(
                f"• {row.run_type} → {name} (✓{row.accept_count} / ✗{row.reject_count})"
            )
        await message.reply_text("\n".join(lines), reply_markup=prefs_actions_keyboard())
        return

    if action == "status":
        user = await get_user_by_telegram_id(tg_user_id)
        if not user:
            await message.reply_text(
                "Not registered. Send /start.",
                reply_markup=main_menu_keyboard(),
            )
            return
        linked = bool(user.strava_athlete_id and user.access_token)
        last_act = await get_last_processed_for_user(user.id)
        lines = [
            f"Telegram ID: {user.telegram_user_id}",
            f"Strava linked: {'yes' if linked else 'no'}",
            f"Strava athlete ID: {user.strava_athlete_id or '—'}",
            f"Skip manually-geared (non-default): {user.skip_if_geared}",
        ]
        if last_act:
            lines.append(
                f"Last assignment: activity {last_act.strava_activity_id} "
                f"[{last_act.status}] type={last_act.run_type} shoe={last_act.shoe_id}"
            )
        await message.reply_text("\n".join(lines), reply_markup=main_menu_keyboard())
        return

    await query.edit_message_text("Unknown menu action.")


def build_application(settings: Settings | None = None) -> Application:
    settings = settings or get_settings()
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.bot_data["settings"] = settings
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", start_command))
    app.add_handler(CommandHandler("link", link_command))
    app.add_handler(CommandHandler("shoes", shoes_command))
    app.add_handler(CommandHandler("recalc", recalc_command))
    app.add_handler(CommandHandler("prefer", prefer_command))
    app.add_handler(CommandHandler("prefs", prefs_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CallbackQueryHandler(shoe_callback, pattern=r"^shoe:"))
    app.add_handler(CallbackQueryHandler(recalc_callback, pattern=r"^recalc:"))
    app.add_handler(CallbackQueryHandler(prefer_callback, pattern=r"^prefer:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_handler))
    return app
