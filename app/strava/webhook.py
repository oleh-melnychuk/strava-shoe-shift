from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.services.assign_shoe import handle_activity_created

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/strava", tags=["strava"])


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_challenge: str = Query(alias="hub.challenge"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
) -> JSONResponse:
    """Strava subscription handshake — echo hub.challenge as JSON."""
    settings = get_settings()
    logger.info(
        "Strava verify: mode=%s token_ok=%s challenge=%s",
        hub_mode,
        hub_verify_token == settings.strava_verify_token,
        hub_challenge,
    )
    if hub_mode == "subscribe" and hub_verify_token == settings.strava_verify_token:
        # Official format: {"hub.challenge":"<challenge>"}
        return JSONResponse(content={"hub.challenge": hub_challenge})
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    force: bool = Query(default=False),
) -> dict[str, str]:
    payload: dict[str, Any] = await request.json()
    # Local replay helper: ?force=true or "force": true in body reprocesses
    # an activity even if it was already handled. Real Strava events never
    # send this, so normal webhooks are unaffected.
    force = force or payload.get("force") is True or str(payload.get("force", "")).lower() == "true"
    logger.info("Strava webhook: %s (force=%s)", payload, force)

    object_type = payload.get("object_type")
    aspect_type = payload.get("aspect_type")
    object_id = payload.get("object_id")
    owner_id = payload.get("owner_id")

    if object_type == "athlete" and payload.get("updates", {}).get("authorized") == "false":
        background_tasks.add_task(_handle_deauth, owner_id)
        return {"status": "ok"}

    if object_type == "activity" and aspect_type == "create" and object_id and owner_id:
        background_tasks.add_task(
            handle_activity_created, int(object_id), int(owner_id), force=force
        )

    return {"status": "ok"}


async def _handle_deauth(athlete_id: int | None) -> None:
    if not athlete_id:
        return
    from app.store import clear_strava_link

    await clear_strava_link(int(athlete_id))
    logger.info("Cleared Strava tokens for athlete %s", athlete_id)
