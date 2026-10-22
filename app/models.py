from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class User:
    telegram_user_id: int
    telegram_chat_id: int
    strava_athlete_id: int | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_expires_at: int | None = None
    skip_if_geared: bool = True
    onboarded_at: str | None = None
    created_at: str = field(default_factory=lambda: _utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: _utcnow().isoformat())

    @property
    def id(self) -> int:
        """Compatibility alias: telegram id is the stable user key."""
        return self.telegram_user_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> User:
        return cls(
            telegram_user_id=int(data["telegram_user_id"]),
            telegram_chat_id=int(data["telegram_chat_id"]),
            strava_athlete_id=int(data["strava_athlete_id"]) if data.get("strava_athlete_id") is not None else None,
            access_token=data.get("access_token"),
            refresh_token=data.get("refresh_token"),
            token_expires_at=int(data["token_expires_at"]) if data.get("token_expires_at") is not None else None,
            skip_if_geared=bool(data.get("skip_if_geared", True)),
            onboarded_at=data.get("onboarded_at"),
            created_at=data.get("created_at") or _utcnow().isoformat(),
            updated_at=data.get("updated_at") or _utcnow().isoformat(),
        )


@dataclass
class ProcessedActivity:
    strava_activity_id: int
    user_id: int
    status: str = "processed"
    run_type: str | None = None
    shoe_id: str | None = None
    confidence: float | None = None
    created_at: str = field(default_factory=lambda: _utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcessedActivity:
        return cls(
            strava_activity_id=int(data["strava_activity_id"]),
            user_id=int(data["user_id"]),
            status=data.get("status") or "processed",
            run_type=data.get("run_type"),
            shoe_id=data.get("shoe_id"),
            confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
            created_at=data.get("created_at") or _utcnow().isoformat(),
        )


@dataclass
class PendingDecision:
    id: int
    user_id: int
    strava_activity_id: int
    run_type: str
    options_json: str
    expires_at: str
    recommended_shoe_id: str | None = None
    reason: str | None = None
    resolved: bool = False
    created_at: str = field(default_factory=lambda: _utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingDecision:
        return cls(
            id=int(data["id"]),
            user_id=int(data["user_id"]),
            strava_activity_id=int(data["strava_activity_id"]),
            run_type=data["run_type"],
            options_json=data["options_json"],
            expires_at=data["expires_at"],
            recommended_shoe_id=data.get("recommended_shoe_id"),
            reason=data.get("reason"),
            resolved=bool(data.get("resolved", False)),
            created_at=data.get("created_at") or _utcnow().isoformat(),
        )


@dataclass
class RunDistanceProfile:
    """User's regular distance range for a run type (from onboarding)."""

    user_id: int
    run_type: str
    min_km: float | None = None
    max_km: float | None = None
    typical_km: float | None = None
    updated_at: str = field(default_factory=lambda: _utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunDistanceProfile:
        def _f(value: Any) -> float | None:
            return float(value) if value is not None else None

        return cls(
            user_id=int(data["user_id"]),
            run_type=data["run_type"],
            min_km=_f(data.get("min_km")),
            max_km=_f(data.get("max_km")),
            typical_km=_f(data.get("typical_km")),
            updated_at=data.get("updated_at") or _utcnow().isoformat(),
        )


@dataclass
class ShoePreference:
    user_id: int
    run_type: str
    shoe_id: str
    shoe_name: str | None = None
    accept_count: int = 0
    reject_count: int = 0
    updated_at: str = field(default_factory=lambda: _utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShoePreference:
        return cls(
            user_id=int(data["user_id"]),
            run_type=data["run_type"],
            shoe_id=data["shoe_id"],
            shoe_name=data.get("shoe_name"),
            accept_count=int(data.get("accept_count") or 0),
            reject_count=int(data.get("reject_count") or 0),
            updated_at=data.get("updated_at") or _utcnow().isoformat(),
        )
