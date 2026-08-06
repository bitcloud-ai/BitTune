from runner.models import ContainerName
from runner.reconciliation import (
    ReconciliationSnapshot,
    RunnerWatchdog,
    StaticReconciliationSource,
    UnavailableReconciliationSource,
)


class _MaintenanceStub:
    def __init__(self, managed: frozenset[ContainerName]) -> None:
        self.managed = managed
        self.maintenance_result: tuple[ContainerName, ...] = ()
        self.reconcile_call: (
            tuple[
                frozenset[ContainerName],
                frozenset[ContainerName],
            ]
            | None
        ) = None

    def watchdog_maintenance(self) -> tuple[ContainerName, ...]:
        return self.maintenance_result

    def reconcile(
        self,
        *,
        expected_container_names: frozenset[ContainerName],
        reconcilable_container_names: frozenset[ContainerName],
    ) -> tuple[ContainerName, ...]:
        self.reconcile_call = (expected_container_names, reconcilable_container_names)
        cleaned = tuple(
            sorted((self.managed - expected_container_names) & reconcilable_container_names)
        )
        self.managed -= frozenset(cleaned)
        return cleaned

    def managed_container_names(self) -> frozenset[ContainerName]:
        return self.managed


def test_missing_authoritative_snapshot_preserves_managed_containers() -> None:
    managed: frozenset[ContainerName] = frozenset({"vllm-unknown"})
    service = _MaintenanceStub(managed)
    report = RunnerWatchdog(
        service=service,
        source=UnavailableReconciliationSource(),
        interval_seconds=5,
    ).run_once()
    assert report.source_available is False
    assert report.cleaned_container_names == ()
    assert report.preserved_unknown_container_names == ("vllm-unknown",)
    assert service.reconcile_call is None


def test_snapshot_cleans_only_persisted_owned_unexpected_containers() -> None:
    expected: ContainerName = "vllm-expected"
    stale: ContainerName = "evalscope-stale"
    unknown: ContainerName = "planner-unknown"
    service = _MaintenanceStub(frozenset({expected, stale, unknown}))
    snapshot = ReconciliationSnapshot(
        schema_version="runner-reconciliation/v1",
        expected_container_names=frozenset({expected}),
        reconcilable_container_names=frozenset({expected, stale}),
    )
    report = RunnerWatchdog(
        service=service,
        source=StaticReconciliationSource(snapshot),
        interval_seconds=5,
    ).run_once()
    assert report.source_available is True
    assert report.cleaned_container_names == (stale,)
    assert report.preserved_unknown_container_names == (unknown,)
    assert service.reconcile_call == (
        snapshot.expected_container_names,
        snapshot.reconcilable_container_names,
    )
