"""Authoritative startup reconciliation and periodic Runner maintenance."""

# Validation messages are part of the typed Runner boundary.
# ruff: noqa: TRY003

from __future__ import annotations

import asyncio
from typing import Protocol, Self

from pydantic import Field, model_validator

from runner.models import ContainerName, RunnerModel

MIN_WATCHDOG_INTERVAL_SECONDS = 0.1
MAX_WATCHDOG_INTERVAL_SECONDS = 300.0


class ReconciliationSnapshot(RunnerModel):
    """Persisted control-plane ownership used to bound destructive cleanup."""

    schema_version: str = Field(pattern=r"^runner-reconciliation/v1$")
    expected_container_names: frozenset[ContainerName] = Field(max_length=4_096)
    reconcilable_container_names: frozenset[ContainerName] = Field(max_length=4_096)

    @model_validator(mode="after")
    def validate_ownership(self) -> Self:
        if not self.expected_container_names <= self.reconcilable_container_names:
            raise ValueError("expected containers must be part of the persisted ownership set")
        return self


class ReconciliationReport(RunnerModel):
    schema_version: str = Field(pattern=r"^runner-reconciliation-report/v1$")
    source_available: bool
    cleaned_container_names: tuple[ContainerName, ...] = Field(max_length=4_096)
    preserved_unknown_container_names: tuple[ContainerName, ...] = Field(max_length=4_096)


class ReconciliationSource(Protocol):
    """Port for a PostgreSQL-backed snapshot supplied by the control plane."""

    def load_snapshot(self) -> ReconciliationSnapshot | None: ...


class RunnerMaintenance(Protocol):
    """Narrow service surface used by the watchdog."""

    def watchdog_maintenance(self) -> tuple[ContainerName, ...]: ...

    def reconcile(
        self,
        *,
        expected_container_names: frozenset[ContainerName],
        reconcilable_container_names: frozenset[ContainerName],
    ) -> tuple[ContainerName, ...]: ...

    def managed_container_names(self) -> frozenset[ContainerName]: ...


class UnavailableReconciliationSource:
    """Fail-safe source: absence of authoritative state never means delete all."""

    def load_snapshot(self) -> None:
        return None


class StaticReconciliationSource:
    """Typed deterministic source for integration and restart tests."""

    def __init__(self, snapshot: ReconciliationSnapshot | None) -> None:
        self.snapshot = snapshot

    def load_snapshot(self) -> ReconciliationSnapshot | None:
        return self.snapshot


class RunnerWatchdog:
    """Run startup reconciliation and repeat deterministic maintenance."""

    def __init__(
        self,
        *,
        service: RunnerMaintenance,
        source: ReconciliationSource,
        interval_seconds: float,
    ) -> None:
        if not MIN_WATCHDOG_INTERVAL_SECONDS <= interval_seconds <= MAX_WATCHDOG_INTERVAL_SECONDS:
            raise ValueError("watchdog interval must be between 0.1 and 300 seconds")
        self._service = service
        self._source = source
        self._interval_seconds = interval_seconds

    def run_once(self) -> ReconciliationReport:
        """Perform one bounded pass, preserving every resource not owned by the snapshot."""

        snapshot = self._source.load_snapshot()
        if snapshot is None:
            cleaned = self._service.watchdog_maintenance()
            preserved = tuple(sorted(self._service.managed_container_names()))
            return ReconciliationReport(
                schema_version="runner-reconciliation-report/v1",
                source_available=False,
                cleaned_container_names=cleaned,
                preserved_unknown_container_names=preserved,
            )

        cleaned = self._service.reconcile(
            expected_container_names=snapshot.expected_container_names,
            reconcilable_container_names=snapshot.reconcilable_container_names,
        )
        observed = self._service.managed_container_names()
        preserved = tuple(sorted(observed - snapshot.reconcilable_container_names))
        return ReconciliationReport(
            schema_version="runner-reconciliation-report/v1",
            source_available=True,
            cleaned_container_names=cleaned,
            preserved_unknown_container_names=preserved,
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run bounded passes until shutdown; failures propagate for systemd restart."""

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                await asyncio.to_thread(self.run_once)
