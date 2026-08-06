# ruff: noqa: TRY003
"""Single-GPU lease management with heartbeat and fencing semantics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol

from runner.errors import (
    GpuLeaseBusyError,
    GpuLeaseExpiredError,
    GpuLeaseNotFoundError,
    StaleFencingTokenError,
)
from runner.models import LeaseResponse, Sha256Digest


class Clock(Protocol):
    """Clock port used to make lease expiry deterministic in tests."""

    def __call__(self) -> datetime: ...


def system_utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class LeaseAcquireResult:
    lease: LeaseResponse
    created: bool


@dataclass(slots=True)
class _LeaseRecord:
    lease_id: str
    owner_id: str
    idempotency_key: Sha256Digest
    fencing_token: int
    heartbeat_at: datetime
    expires_at: datetime

    def response(self) -> LeaseResponse:
        return LeaseResponse(
            lease_id=self.lease_id,
            owner_id=self.owner_id,
            fencing_token=self.fencing_token,
            heartbeat_at=self.heartbeat_at,
            expires_at=self.expires_at,
        )


class GpuLeaseManager:
    """Own the only GPU 0 lease and reject stale owners with fencing tokens."""

    def __init__(self, *, clock: Clock = system_utc_now) -> None:
        self._clock = clock
        self._mutex = Lock()
        self._active: _LeaseRecord | None = None
        self._generation = 0
        self._cleanup_generation: int | None = None

    def acquire(
        self,
        *,
        lease_id: str,
        owner_id: str,
        idempotency_key: Sha256Digest,
        duration: timedelta,
    ) -> LeaseAcquireResult:
        """Acquire GPU 0, replaying the same idempotent request."""

        self._require_positive_duration(duration)
        with self._mutex:
            now = self._now()
            active = self._active
            if active is not None and active.expires_at <= now:
                raise GpuLeaseExpiredError(
                    "expired GPU lease requires successful reconciliation before acquisition"
                )
            if active is not None:
                if (
                    active.lease_id == lease_id
                    and active.owner_id == owner_id
                    and active.idempotency_key == idempotency_key
                ):
                    return LeaseAcquireResult(active.response(), created=False)
                raise GpuLeaseBusyError("GPU 0 is held by another active lease")

            self._generation += 1
            record = _LeaseRecord(
                lease_id=lease_id,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
                fencing_token=self._generation,
                heartbeat_at=now,
                expires_at=now + duration,
            )
            self._active = record
            return LeaseAcquireResult(record.response(), created=True)

    def heartbeat(
        self,
        *,
        lease_id: str,
        owner_id: str,
        fencing_token: int,
        duration: timedelta,
    ) -> LeaseResponse:
        """Extend an active lease after validating owner and fencing token."""

        self._require_positive_duration(duration)
        with self._mutex:
            now = self._now()
            record = self._require_active(now)
            self._require_identity(record, lease_id, owner_id, fencing_token)
            record.heartbeat_at = now
            record.expires_at = now + duration
            return record.response()

    def release(
        self,
        *,
        lease_id: str,
        owner_id: str,
        fencing_token: int,
    ) -> bool:
        """Release the active lease.  Releasing an already absent lease is idempotent."""

        with self._mutex:
            now = self._now()
            if self._active is None:
                return False
            if self._active.expires_at <= now:
                raise GpuLeaseExpiredError("GPU lease expired before release")
            self._require_identity(self._active, lease_id, owner_id, fencing_token)
            self._active = None
            return True

    def current(self) -> LeaseResponse | None:
        """Return the active lease, retaining expired state for reconciliation."""

        with self._mutex:
            now = self._now()
            if self._active is not None and self._active.expires_at <= now:
                return None
            return None if self._active is None else self._active.response()

    def expire_and_cleanup(self, cleanup: Callable[[LeaseResponse], None]) -> bool:
        """Remove an expired lease and invoke deterministic resource cleanup once."""

        expired: LeaseResponse | None = None
        expired_generation: int | None = None
        with self._mutex:
            now = self._now()
            if self._active is not None and self._active.expires_at <= now:
                if self._cleanup_generation == self._active.fencing_token:
                    return False
                expired = self._active.response()
                expired_generation = self._active.fencing_token
                self._cleanup_generation = expired_generation
        if expired is None:
            return False
        succeeded = False
        try:
            cleanup(expired)
            succeeded = True
        finally:
            with self._mutex:
                if (
                    succeeded
                    and self._active is not None
                    and self._active.fencing_token == expired_generation
                    and self._active.expires_at <= self._now()
                ):
                    self._active = None
                if self._cleanup_generation == expired_generation:
                    self._cleanup_generation = None
        return True

    def _require_active(self, now: datetime) -> _LeaseRecord:
        if self._active is None:
            raise GpuLeaseNotFoundError("GPU lease does not exist")
        if self._active.expires_at <= now:
            raise GpuLeaseExpiredError("GPU lease has expired")
        return self._active

    @staticmethod
    def _require_identity(
        record: _LeaseRecord,
        lease_id: str,
        owner_id: str,
        fencing_token: int,
    ) -> None:
        if record.fencing_token != fencing_token:
            raise StaleFencingTokenError("GPU lease fencing token is stale")
        if record.lease_id != lease_id or record.owner_id != owner_id:
            raise GpuLeaseNotFoundError("GPU lease owner does not match")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("lease clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _require_positive_duration(duration: timedelta) -> None:
        if duration <= timedelta(0):
            raise ValueError("lease duration must be positive")
