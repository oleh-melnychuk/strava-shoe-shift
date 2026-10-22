from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import ContextTypes
from telegram import Update

from app.ai.classifier import (
    ONBOARDING_RUN_TYPES,
    RUN_DISTANCE_PRESETS,
    format_distance_profile,
    next_onboarding_run_type,
    normalize_run_type,
)
from app.models import User
from app.store import (
    list_preferences,
    list_run_distances,
    save_user,
    set_explicit_preference,
    set_run_distance,
)
from app.strava.client import get_client_for_user, get_user_by_telegram_id
from app.telegram.keyboards import main_menu_keyboard

logger = logging.getLogger(__name__)

TYPE_BLURB = {
    "recovery": "easy / recovery",
    "tempo": "tempo / threshold",
    "intervals": "intervals / track",
    "long": "long run",
    "race": "race",
}


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def _step_index(run_type: str) -> int:
    try:
        return ONBOARDING_RUN_TYPES.index(run_type) + 1
    except ValueError:
        return 1


def _shoe_keyboard(run_type: str, shoes: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=(shoe.get("name") or shoe["id"])[:64],
                callback_data=f"onb:s:{run_type}:{shoe['id']}",
            )
        ]
        for shoe in shoes[:10]
        if shoe.get("id")
    ]
    rows.append(
        [
            InlineKeyboardButton("Skip shoe", callback_data=f"onb:s:{run_type}:_"),
            InlineKeyboardButton("Finish", callback_data="onb:done"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _distance_keyboard(run_type: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for index, (label, _min, _max, _typ) in enumerate(RUN_DISTANCE_PRESETS):
        pair.append(
            InlineKeyboardButton(label, callback_data=f"onb:d:{run_type}:{index}")
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append(
        [
            InlineKeyboardButton("Skip distance", callback_data=f"onb:d:{run_type}:_"),
            InlineKeyboardButton("Finish", callback_data="onb:done"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _active_shoes(user: User) -> list[dict]:
    client = await get_client_for_user(user)
    athlete = await client.get_athlete()
    shoes = [s for s in (athlete.get("shoes") or []) if s.get("id") and not s.get("retired")]
    if not shoes:
        shoes = [s for s in (athlete.get("shoes") or []) if s.get("id")]
    return shoes


async def format_profile_summary(user_id: int) -> str:
    prefs = await list_preferences(user_id)
    dists = await list_run_distances(user_id)
    pref_by_type = {p.run_type: p for p in prefs}
    dist_by_type = {d.run_type: d for d in dists}

    lines = ["Your setup:"]
    any_row = False
    for run_type in ONBOARDING_RUN_TYPES:
        shoe = pref_by_type.get(run_type)
        dist = dist_by_type.get(run_type)
        if not shoe and not dist:
            continue
        any_row = True
        shoe_bit = (shoe.shoe_name or shoe.shoe_id) if shoe else "no shoe pinned"
        dist_bit = format_distance_profile(dist.to_dict()) if dist else "no usual distance"
        lines.append(f"• {run_type} → {shoe_bit}, {dist_bit}")
    if not any_row:
        return "No shoes or distances saved yet. Tap Setup to add them."
    return "\n".join(lines)


async def _mark_onboarded(user: User) -> None:
    user.onboarded_at = datetime.now(timezone.utc).isoformat()
    await save_user(user)


async def _reply(
    *,
    query,
    message: Message | None,
    bot: Bot | None,
    chat_id: int | None,
    text: str,
    reply_markup=None,
    parse_mode: str = "HTML",
) -> None:
    if query:
        await query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )
        return
    if message:
        await message.reply_text(
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        return
    if bot and chat_id is not None:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )


async def prompt_shoe_step(
    run_type: str,
    shoes: list[dict],
    *,
    query=None,
    message: Message | None = None,
    bot: Bot | None = None,
    chat_id: int | None = None,
    prefix: str = "",
) -> None:
    blurb = TYPE_BLURB.get(run_type, run_type)
    step = _step_index(run_type)
    total = len(ONBOARDING_RUN_TYPES)
    text = (
        f"{prefix}Setup {step}/{total} — <b>{_esc(run_type)}</b>\n\n"
        f"Usual shoe for {blurb} runs?"
    )
    await _reply(
        query=query,
        message=message,
        bot=bot,
        chat_id=chat_id,
        text=text,
        reply_markup=_shoe_keyboard(run_type, shoes),
    )


async def prompt_distance_step(
    run_type: str,
    *,
    query=None,
    message: Message | None = None,
    bot: Bot | None = None,
    chat_id: int | None = None,
    prefix: str = "",
) -> None:
    blurb = TYPE_BLURB.get(run_type, run_type)
    step = _step_index(run_type)
    total = len(ONBOARDING_RUN_TYPES)
    text = (
        f"{prefix}Setup {step}/{total} — <b>{_esc(run_type)}</b>\n\n"
        f"Typical distance for {blurb} runs?"
    )
    await _reply(
        query=query,
        message=message,
        bot=bot,
        chat_id=chat_id,
        text=text,
        reply_markup=_distance_keyboard(run_type),
    )


async def finish_onboarding(
    user: User,
    *,
    query=None,
    message: Message | None = None,
    bot: Bot | None = None,
) -> None:
    await _mark_onboarded(user)
    summary = await format_profile_summary(user.id)
    text = (
        "Setup saved. I'll use these pins and distances to classify new runs "
        "and pick shoes.\n\n"
        f"{_esc(summary)}"
    )
    await _reply(
        query=query,
        message=message,
        bot=bot,
        chat_id=user.telegram_chat_id,
        text=text,
    )
    # Persistent reply keyboard is separate from the edited inline message.
    target_bot = bot or (query.get_bot() if query else None)
    if target_bot:
        await target_bot.send_message(
            chat_id=user.telegram_chat_id,
            text="You can redo this anytime with Setup.",
            reply_markup=main_menu_keyboard(),
        )


async def start_onboarding(
    user: User,
    *,
    query=None,
    message: Message | None = None,
    bot: Bot | None = None,
) -> None:
    if not user.access_token:
        await _reply(
            query=query,
            message=message,
            bot=bot,
            chat_id=user.telegram_chat_id,
            text="Link Strava first — tap Link Strava.",
            reply_markup=main_menu_keyboard() if not query else None,
            parse_mode="HTML",
        )
        return
    try:
        shoes = await _active_shoes(user)
    except Exception:
        logger.exception("Failed to fetch shoes for onboarding")
        await _reply(
            query=query,
            message=message,
            bot=bot,
            chat_id=user.telegram_chat_id,
            text="Could not fetch shoes from Strava. Try Setup again in a moment.",
        )
        return
    if not shoes:
        await _reply(
            query=query,
            message=message,
            bot=bot,
            chat_id=user.telegram_chat_id,
            text="No shoes on your Strava profile. Add shoes there, then tap Setup.",
        )
        return

    first = ONBOARDING_RUN_TYPES[0]
    intro = (
        "Tell me your usual shoe and typical distance for each run type. "
        "Skip keeps whatever you already have.\n\n"
    )
    await prompt_shoe_step(
        first,
        shoes,
        query=query,
        message=message,
        bot=bot,
        chat_id=user.telegram_chat_id,
        prefix=intro,
    )


async def setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = await get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text(
            "Not registered yet — send /start, then Link Strava.",
            reply_markup=main_menu_keyboard(),
        )
        return
    await start_onboarding(user, message=update.message, bot=context.bot)


async def onboarding_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return
    await query.answer()

    user = await get_user_by_telegram_id(query.from_user.id)
    if not user:
        await query.edit_message_text("Not registered. Send /start.")
        return

    parts = query.data.split(":")
    if len(parts) < 2 or parts[0] != "onb":
        await query.edit_message_text("Invalid setup step.")
        return

    action = parts[1]
    if action == "go":
        await start_onboarding(user, query=query, bot=context.bot)
        return
    if action == "done":
        await finish_onboarding(user, query=query, bot=context.bot)
        return

    if action not in {"s", "d"} or len(parts) < 4:
        await query.edit_message_text("Invalid setup step.")
        return

    run_type = normalize_run_type(parts[2])
    if run_type not in ONBOARDING_RUN_TYPES:
        await query.edit_message_text("Unknown run type.")
        return
    value = ":".join(parts[3:])

    if action == "s":
        if value != "_":
            shoe_name = value
            try:
                shoes = await _active_shoes(user)
                for shoe in shoes:
                    if shoe.get("id") == value:
                        shoe_name = shoe.get("name") or value
                        break
            except Exception:
                logger.exception("Failed to resolve shoe name during onboarding")
                shoes = []
            await set_explicit_preference(user.id, run_type, value, shoe_name)
            prefix = f"Saved <b>{_esc(shoe_name)}</b> for {_esc(run_type)}.\n\n"
        else:
            prefix = f"No shoe pinned for {_esc(run_type)}.\n\n"
        await prompt_distance_step(run_type, query=query, prefix=prefix)
        return

    # Distance step.
    if value != "_":
        try:
            index = int(value)
            label, min_km, max_km, typical_km = RUN_DISTANCE_PRESETS[index]
        except (ValueError, IndexError):
            await query.edit_message_text("Invalid distance choice.")
            return
        await set_run_distance(user.id, run_type, min_km, max_km, typical_km)
        prefix = f"Saved {_esc(label)} for {_esc(run_type)}.\n\n"
    else:
        prefix = f"No usual distance for {_esc(run_type)}.\n\n"

    nxt = next_onboarding_run_type(run_type)
    if not nxt:
        await finish_onboarding(user, query=query, bot=context.bot)
        return
    try:
        shoes = await _active_shoes(user)
    except Exception:
        logger.exception("Failed to fetch shoes for next onboarding step")
        await finish_onboarding(user, query=query, bot=context.bot)
        return
    await prompt_shoe_step(nxt, shoes, query=query, prefix=prefix)


async def start_onboarding_after_link(user: User, athlete_name: str, bot_token: str) -> None:
    bot = Bot(token=bot_token)
    await bot.send_message(
        chat_id=user.telegram_chat_id,
        text=(
            f"Linked Strava account for <b>{_esc(athlete_name)}</b>.\n"
            "Next: usual shoe + typical distance for each run type."
        ),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )
    await start_onboarding(user, bot=bot)
