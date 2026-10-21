from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.services.assign_shoe import handle_activity_created

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/strava", tags=["strava"])


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_challenge: str = Query(alias="hub.challenge"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
) -> PlainTextResponse:
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.strava_verify_token:
        return PlainTextResponse(content=hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    payload: dict[str, Any] = await request.json()
    logger.info("Strava webhook: %s", payload)

    object_type = payload.get("object_type")
    aspect_type = payload.get("aspect_type")
    object_id = payload.get("object_id")
    owner_id = payload.get("owner_id")

    if object_type == "athlete" and payload.get("updates", {}).get("authorized") == "false":
        background_tasks.add_task(_handle_deauth, owner_id)
        return {"status": "ok"}

    if object_type == "activity" and aspect_type == "create" and object_id and owner_id:
        background_tasks.add_task(handle_activity_created, int(object_id), int(owner_id))

    return {"status": "ok"}


async def _handle_deauth(athlete_id: int | None) -> None:
    if not athlete_id:
        return
    from app.store import clear_strava_link

    await clear_strava_link(int(athlete_id))
    logger.info("Cleared Strava tokens for athlete %s", athlete_id)
