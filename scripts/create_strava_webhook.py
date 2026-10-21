#!/usr/bin/env python3
"""Create or view a Strava push subscription.

Usage:
  python scripts/create_strava_webhook.py create
  python scripts/create_strava_webhook.py view
  python scripts/create_strava_webhook.py delete <subscription_id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

SUBSCRIPTIONS_URL = "https://www.strava.com/api/v3/push_subscriptions"


def create_subscription() -> None:
    settings = get_settings()
    callback_url = f"{settings.public_base_url.rstrip('/')}/strava/webhook"
    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            SUBSCRIPTIONS_URL,
            data={
                "client_id": settings.strava_client_id,
                "client_secret": settings.strava_client_secret,
                "callback_url": callback_url,
                "verify_token": settings.strava_verify_token,
            },
        )
        print(response.status_code, response.text)
        response.raise_for_status()


def view_subscriptions() -> None:
    settings = get_settings()
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            SUBSCRIPTIONS_URL,
            params={
                "client_id": settings.strava_client_id,
                "client_secret": settings.strava_client_secret,
            },
        )
        print(response.status_code, response.text)
        response.raise_for_status()


def delete_subscription(subscription_id: int) -> None:
    settings = get_settings()
    with httpx.Client(timeout=30.0) as client:
        response = client.delete(
            f"{SUBSCRIPTIONS_URL}/{subscription_id}",
            params={
                "client_id": settings.strava_client_id,
                "client_secret": settings.strava_client_secret,
            },
        )
        print(response.status_code, response.text)
        response.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Strava webhook subscription")
    parser.add_argument("action", choices=["create", "view", "delete"])
    parser.add_argument("subscription_id", nargs="?", type=int)
    args = parser.parse_args()

    if args.action == "create":
        create_subscription()
    elif args.action == "view":
        view_subscriptions()
    else:
        if not args.subscription_id:
            parser.error("delete requires subscription_id")
        delete_subscription(args.subscription_id)


if __name__ == "__main__":
    main()
