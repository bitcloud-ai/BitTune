from datetime import UTC, datetime, timedelta

import pytest

from runner.errors import GpuLeaseBusyError, GpuLeaseExpiredError, StaleFencingTokenError
from runner.leases import GpuLeaseManager
from runner.models import Sha256Digest


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 6, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_gpu_lease_is_exclusive_idempotent_and_fenced() -> None:
    clock = MutableClock()
    manager = GpuLeaseManager(clock=clock)
    digest = Sha256Digest(root="sha256:" + "a" * 64)
    first = manager.acquire(
        lease_id="gpu-lease-one",
        owner_id="worker_" + "1" * 32,
        idempotency_key=digest,
        duration=timedelta(seconds=30),
    )
    replay = manager.acquire(
        lease_id="gpu-lease-one",
        owner_id="worker_" + "1" * 32,
        idempotency_key=digest,
        duration=timedelta(seconds=30),
    )
    assert first.created is True
    assert replay.created is False
    assert replay.lease.fencing_token == first.lease.fencing_token

    with pytest.raises(GpuLeaseBusyError):
        manager.acquire(
            lease_id="gpu-lease-two",
            owner_id="worker_" + "2" * 32,
            idempotency_key=Sha256Digest(root="sha256:" + "b" * 64),
            duration=timedelta(seconds=30),
        )

    with pytest.raises(StaleFencingTokenError):
        manager.heartbeat(
            lease_id="gpu-lease-one",
            owner_id="worker_" + "1" * 32,
            fencing_token=first.lease.fencing_token + 1,
            duration=timedelta(seconds=30),
        )


def test_expired_lease_cleans_once_and_increments_fencing_generation() -> None:
    clock = MutableClock()
    manager = GpuLeaseManager(clock=clock)
    first = manager.acquire(
        lease_id="gpu-lease-one",
        owner_id="worker_" + "1" * 32,
        idempotency_key=Sha256Digest(root="sha256:" + "a" * 64),
        duration=timedelta(seconds=10),
    )
    clock.now += timedelta(seconds=10)
    cleaned: list[str] = []
    assert manager.expire_and_cleanup(lambda lease: cleaned.append(lease.lease_id)) is True
    assert manager.expire_and_cleanup(lambda lease: cleaned.append(lease.lease_id)) is False
    assert cleaned == ["gpu-lease-one"]

    second = manager.acquire(
        lease_id="gpu-lease-two",
        owner_id="worker_" + "2" * 32,
        idempotency_key=Sha256Digest(root="sha256:" + "b" * 64),
        duration=timedelta(seconds=10),
    )
    assert second.lease.fencing_token > first.lease.fencing_token


def test_expired_heartbeat_is_rejected() -> None:
    clock = MutableClock()
    manager = GpuLeaseManager(clock=clock)
    acquired = manager.acquire(
        lease_id="gpu-lease-one",
        owner_id="worker_" + "1" * 32,
        idempotency_key=Sha256Digest(root="sha256:" + "a" * 64),
        duration=timedelta(seconds=10),
    )
    clock.now += timedelta(seconds=11)
    with pytest.raises(GpuLeaseExpiredError):
        manager.heartbeat(
            lease_id="gpu-lease-one",
            owner_id="worker_" + "1" * 32,
            fencing_token=acquired.lease.fencing_token,
            duration=timedelta(seconds=10),
        )
