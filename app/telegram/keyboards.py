from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

# Reply keyboard labels (must match MessageHandler routing)
BTN_LINK = "Link Strava"
BTN_SHOES = "Shoes"
BTN_RECALC = "Recalc"
BTN_SETUP = "Setup"
BTN_PREFER = "Prefer"
BTN_PREFS = "Prefs"
BTN_STATUS = "Status"
BTN_MENU = "Menu"

MAIN_BUTTONS = (
    BTN_RECALC,
    BTN_SHOES,
    BTN_LINK,
    BTN_SETUP,
    BTN_PREFS,
    BTN_STATUS,
)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_RECALC), KeyboardButton(BTN_SHOES)],
            [KeyboardButton(BTN_LINK), KeyboardButton(BTN_STATUS)],
            [KeyboardButton(BTN_SETUP), KeyboardButton(BTN_PREFS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def prefs_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Redo setup", callback_data="menu:setup"),
                InlineKeyboardButton("Clear prefs", callback_data="menu:prefs_clear"),
            ]
        ]
    )


def home_inline_keyboard() -> InlineKeyboardMarkup:
    """Optional inline shortcuts under welcome message."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Recalc", callback_data="menu:recalc"),
                InlineKeyboardButton("Shoes", callback_data="menu:shoes"),
            ],
            [
                InlineKeyboardButton("Link Strava", callback_data="menu:link"),
                InlineKeyboardButton("Status", callback_data="menu:status"),
            ],
            [
                InlineKeyboardButton("Setup", callback_data="menu:setup"),
                InlineKeyboardButton("Prefs", callback_data="menu:prefs"),
            ],
        ]
    )
