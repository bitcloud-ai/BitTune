from pathlib import Path

import pytest

from runner.errors import PathBoundaryError, RunnerConfigurationError
from runner.models import StorageRef, StorageRoot
from runner.paths import RootRegistry


def test_registry_requires_every_distinct_absolute_root(tmp_path: Path) -> None:
    with pytest.raises(RunnerConfigurationError):
        RootRegistry({StorageRoot.OUTPUT: tmp_path / "output"})

    relative = Path("relative-model-cache")
    with pytest.raises(RunnerConfigurationError):
        RootRegistry(
            {
                StorageRoot.MODEL_CACHE: relative,
                StorageRoot.OUTPUT: tmp_path / "output",
                StorageRoot.TEMPORARY: tmp_path / "temporary",
                StorageRoot.RUNTIME: tmp_path / "runtime",
            }
        )


def test_resolve_and_prepare_stay_under_registered_root(roots: RootRegistry) -> None:
    reference = StorageRef.model_validate({"root": "temporary", "relative_path": "experiments/one"})
    resolved = roots.prepare_directory(reference)
    assert resolved == roots.root(StorageRoot.TEMPORARY) / "experiments" / "one"
    assert resolved.is_dir()


def test_symlink_escape_is_rejected(roots: RootRegistry, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = roots.root(StorageRoot.TEMPORARY) / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating symbolic links is unavailable in this Windows environment")

    reference = StorageRef.model_validate({"root": "temporary", "relative_path": "escape/data"})
    with pytest.raises(PathBoundaryError):
        roots.resolve(reference)
