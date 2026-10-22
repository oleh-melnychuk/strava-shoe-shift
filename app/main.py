from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from telegram import Update
from telegram.ext import Application

from app.config import get_settings
from app.db import close_db, init_db
from app.store import get_user_by_telegram_id, save_user
from app.strava import webhook as strava_webhook
from app.strava.oauth import exchange_code
from app.telegram.bot import build_application
from app.telegram.handlers import resolve_link_notify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
telegram_app: Application | None = None


def get_telegram_app() -> Application:
    global telegram_app
    if telegram_app is None:
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        telegram_app = build_application(settings)
    return telegram_app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()

    tg_app = get_telegram_app()
    await tg_app.initialize()

    polling_task: asyncio.Task | None = None
    if settings.telegram_polling:
        await tg_app.start()
        assert tg_app.updater is not None
        polling_task = asyncio.create_task(tg_app.updater.start_polling(drop_pending_updates=True))
        logger.info("Telegram polling started")
    else:
        webhook_url = f"{settings.public_base_url.rstrip('/')}/telegram/webhook"
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=settings.telegram_webhook_secret,
            drop_pending_updates=True,
        )
        await tg_app.start()
        logger.info("Telegram webhook set to %s", webhook_url)

    yield

    if settings.telegram_polling and tg_app.updater:
        await tg_app.updater.stop()
    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
    if not settings.telegram_polling:
        await tg_app.bot.delete_webhook()
    await tg_app.stop()
    await tg_app.shutdown()
    await close_db()


app = FastAPI(title="Strava Shoe Shift", lifespan=lifespan)
app.include_router(strava_webhook.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/oauth/strava/callback")
async def strava_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    if error:
        raise HTTPException(status_code=400, detail=f"Strava OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    try:
        telegram_user_id = int(state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid state") from exc

    token_data = await exchange_code(settings, code)
    athlete = token_data.get("athlete") or {}
    athlete_id = athlete.get("id")
    athlete_name = " ".join(
        filter(None, [athlete.get("firstname"), athlete.get("lastname")])
    ) or f"athlete {athlete_id}"

    user = await get_user_by_telegram_id(telegram_user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Start the bot with /link first")

    user.strava_athlete_id = athlete_id
    user.access_token = token_data["access_token"]
    user.refresh_token = token_data["refresh_token"]
    user.token_expires_at = token_data["expires_at"]
    await save_user(user)

    try:
        await resolve_link_notify(user, athlete_name, settings.telegram_bot_token)
    except Exception:
        logger.exception("Failed to notify Telegram after OAuth")

    return HTMLResponse(
        content=(
            "<html><body style='font-family:sans-serif;padding:2rem'>"
            f"<h1>Connected</h1><p>Strava account linked for {athlete_name}. "
            "You can close this window and return to Telegram.</p>"
            "</body></html>"
        )
    )


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, str]:
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if header_secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid secret")

    tg_app = get_telegram_app()
    payload = await request.json()
    update = Update.de_json(payload, tg_app.bot)
    await tg_app.process_update(update)
    return {"status": "ok"}
