# ruff: noqa: N802, N815, TRY003, TRY301
"""Pinned NVML and native Linux collectors.

The adapter only reads the NVML field subset declared by the domain contract.
It never calls NVML setters and never executes a shell command.
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
from pathlib import Path
from typing import Literal, Protocol, cast

from autopilot.capabilities.environment.domain.enums import EnvironmentValidationCode
from autopilot.capabilities.environment.domain.errors import EnvironmentProviderUnavailableError
from autopilot.capabilities.environment.domain.models import GpuSnapshot, HostSnapshot
from autopilot.domain.base import utc_now
from autopilot.domain.hardware import (
    GpuProcess,
    HostCpu,
    HostMemory,
    HostOs,
    RuntimeVersions,
    StorageVolume,
)


class _NvmlMemory(Protocol):
    total: int
    free: int


class _NvmlUtilization(Protocol):
    gpu: int


class _NvmlProcess(Protocol):
    pid: int
    usedGpuMemory: int | None


class _NvmlApi(Protocol):
    NVML_TEMPERATURE_GPU: int

    def nvmlInit(self) -> None: ...

    def nvmlShutdown(self) -> None: ...

    def nvmlSystemGetDriverVersion(self) -> str | bytes: ...

    def nvmlDeviceGetCount(self) -> int: ...

    def nvmlDeviceGetHandleByIndex(self, index: int) -> object: ...

    def nvmlDeviceGetName(self, handle: object) -> str | bytes: ...

    def nvmlDeviceGetUUID(self, handle: object) -> str | bytes: ...

    def nvmlDeviceGetMemoryInfo(self, handle: object) -> _NvmlMemory: ...

    def nvmlDeviceGetTemperature(self, handle: object, sensor: int) -> int: ...

    def nvmlDeviceGetUtilizationRates(self, handle: object) -> _NvmlUtilization: ...

    def nvmlDeviceGetPowerUsage(self, handle: object) -> int: ...

    def nvmlDeviceGetComputeRunningProcesses(self, handle: object) -> list[_NvmlProcess]: ...


def _text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


class PynvmlCollector:
    """Read GPU 0 through the optional, pinned ``nvidia-ml-py`` binding."""

    def __init__(self, api: _NvmlApi | None = None) -> None:
        self._injected_api = api

    @property
    def driver_version(self) -> str:
        api = self._api()
        try:
            api.nvmlInit()
            return _text(api.nvmlSystemGetDriverVersion())
        except Exception as error:
            raise EnvironmentProviderUnavailableError(
                "NVML could not read the driver version",
                EnvironmentValidationCode.NVML_ERROR,
            ) from error
        finally:
            self._shutdown(api)

    def collect_gpu_zero(self) -> GpuSnapshot:
        api = self._api()
        try:
            api.nvmlInit()
            count = api.nvmlDeviceGetCount()
            if count != 1:
                raise EnvironmentProviderUnavailableError(
                    "the MVP requires exactly one visible NVIDIA GPU",
                    EnvironmentValidationCode.GPU_COUNT_UNSUPPORTED,
                )
            handle = api.nvmlDeviceGetHandleByIndex(0)
            memory = api.nvmlDeviceGetMemoryInfo(handle)
            utilization = api.nvmlDeviceGetUtilizationRates(handle)
            power_watts = self._read_power_watts(api, handle)
            processes = tuple(
                GpuProcess(
                    process_id=process.pid,
                    used_memory_bytes=process.usedGpuMemory or 0,
                )
                for process in api.nvmlDeviceGetComputeRunningProcesses(handle)
            )
            return GpuSnapshot(
                name=_text(api.nvmlDeviceGetName(handle)),
                uuid=_text(api.nvmlDeviceGetUUID(handle)),
                memory_total_bytes=memory.total,
                memory_free_bytes=memory.free,
                temperature_celsius=float(
                    api.nvmlDeviceGetTemperature(handle, api.NVML_TEMPERATURE_GPU)
                ),
                utilization_percent=float(utilization.gpu),
                power_watts=power_watts,
                processes=processes,
            )
        except EnvironmentProviderUnavailableError:
            raise
        except Exception as error:
            raise EnvironmentProviderUnavailableError(
                "NVML environment inspection failed",
                EnvironmentValidationCode.NVML_ERROR,
            ) from error
        finally:
            self._shutdown(api)

    def _api(self) -> _NvmlApi:
        if self._injected_api is not None:
            return self._injected_api
        try:
            module = importlib.import_module("pynvml")
        except ImportError as error:
            raise EnvironmentProviderUnavailableError(
                "the pinned nvidia-ml-py binding is not installed"
            ) from error
        return cast(_NvmlApi, module)

    @staticmethod
    def _read_power_watts(api: _NvmlApi, handle: object) -> float | None:
        try:
            return api.nvmlDeviceGetPowerUsage(handle) / 1_000
        except Exception as error:
            if error.__class__.__name__ == "NVMLError_NotSupported":
                return None
            raise

    @staticmethod
    def _shutdown(api: _NvmlApi) -> None:
        try:
            api.nvmlShutdown()
        except Exception:
            return


class NativeLinuxHostCollector:
    """Collect host facts using Python and procfs only."""

    def __init__(
        self,
        *,
        storage_root: Path,
        docker_version: str | None,
        compose_version: str | None,
        nvidia_container_toolkit_version: str | None,
        gpu_container_probe: Literal["passed", "failed", "not_run"],
    ) -> None:
        self._storage_root = storage_root
        self._docker_version = docker_version
        self._compose_version = compose_version
        self._toolkit_version = nvidia_container_toolkit_version
        self._gpu_container_probe = gpu_container_probe

    def collect(self, *, gpu: GpuSnapshot, driver_version: str) -> HostSnapshot:
        if platform.system() != "Linux":
            raise EnvironmentProviderUnavailableError(
                "the native host collector only runs on Linux",
                EnvironmentValidationCode.NON_LINUX_HOST,
            )
        try:
            os_release = platform.freedesktop_os_release()
            memory = self._read_memory()
            storage = shutil.disk_usage(self._storage_root)
        except (OSError, ValueError, KeyError, IndexError) as error:
            raise EnvironmentProviderUnavailableError(
                "native Linux host facts could not be collected",
                EnvironmentValidationCode.INVALID_SNAPSHOT,
            ) from error
        return HostSnapshot(
            os=HostOs(
                name=os_release.get("NAME", "Linux"),
                version=os_release.get("VERSION_ID", platform.release()),
                kernel=platform.release(),
                architecture=platform.machine(),
            ),
            cpu=HostCpu(
                model=self._cpu_model(),
                logical_cores=os.cpu_count() or 1,
            ),
            memory=memory,
            storage=(
                StorageVolume(
                    volume_id="model-cache",
                    total_bytes=storage.total,
                    available_bytes=storage.free,
                ),
            ),
            runtime=RuntimeVersions(
                driver_version=driver_version,
                docker_version=self._docker_version,
                compose_version=self._compose_version,
                nvidia_container_toolkit_version=self._toolkit_version,
                gpu_container_probe=self._gpu_container_probe,
            ),
            gpu=gpu,
            captured_at=utc_now(),
        )

    @staticmethod
    def _read_memory() -> HostMemory:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw_value = line.split(":", maxsplit=1)
            first_value = raw_value.strip().split(maxsplit=1)[0]
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(first_value) * 1_024
        return HostMemory(
            total_bytes=values["MemTotal"],
            available_bytes=values["MemAvailable"],
        )

    @staticmethod
    def _cpu_model() -> str:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", maxsplit=1)[1].strip()
        return platform.processor() or platform.machine()
