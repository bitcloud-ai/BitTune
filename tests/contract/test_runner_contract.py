from pathlib import Path

from runner.docker import DockerAdapter
from runner.fakes import FakeDockerAdapter


def test_fake_docker_implements_only_typed_lifecycle_surface() -> None:
    fake = FakeDockerAdapter()
    for method in ("create", "start", "stop", "remove", "inspect", "list_managed"):
        assert callable(getattr(fake, method))
    for forbidden in ("run", "exec", "shell", "execute", "mount"):
        assert not hasattr(fake, forbidden)


def test_docker_port_has_no_generic_execution_method() -> None:
    public = set(DockerAdapter.__dict__)
    assert {"create", "start", "stop", "remove", "inspect", "list_managed"} <= public
    assert {"run", "exec", "shell", "execute"}.isdisjoint(public)


def test_systemd_unit_hardens_runner_and_allows_only_unix_address_family() -> None:
    unit = (
        Path(__file__).parents[2] / "runner" / "systemd" / "autopilot-runner.service"
    ).read_text(encoding="utf-8")
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "IPAddressDeny=any" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "CapabilityBoundingSet=" in unit
    assert "--privileged" not in unit
