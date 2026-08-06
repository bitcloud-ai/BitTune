"""Deployment-time configuration for the standalone host runner."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from runner.docker import ContainerKind, ContainerSpecCompiler, RunnerDockerPolicy
from runner.models import ImageDigest, StorageRoot
from runner.paths import RootRegistry


class RunnerSettings(BaseSettings):
    """Trusted runner settings supplied by systemd, never by an Agent request."""

    model_config = SettingsConfigDict(
        env_prefix="AUTOPILOT_RUNNER_",
        extra="forbid",
        frozen=True,
    )

    runtime_root: Path
    model_cache_root: Path
    output_root: Path
    temp_root: Path
    vllm_image: ImageDigest
    evalscope_image: ImageDigest
    planner_image: ImageDigest
    network: str = Field(default="autopilot-runtime", pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    maintenance_interval_seconds: float = Field(default=5.0, ge=0.1, le=300.0)

    def compiler(self) -> ContainerSpecCompiler:
        roots = RootRegistry(
            {
                StorageRoot.MODEL_CACHE: self.model_cache_root,
                StorageRoot.OUTPUT: self.output_root,
                StorageRoot.TEMPORARY: self.temp_root,
                StorageRoot.RUNTIME: self.runtime_root,
            }
        )
        policy = RunnerDockerPolicy(
            network=self.network,
            allowed_images={
                ContainerKind.VLLM: frozenset({self.vllm_image}),
                ContainerKind.EVALSCOPE: frozenset({self.evalscope_image}),
                ContainerKind.PLANNER: frozenset({self.planner_image}),
                ContainerKind.CUDA_PROBE: frozenset(),
            },
            evalscope_image=self.evalscope_image,
            planner_image=self.planner_image,
        )
        return ContainerSpecCompiler(roots=roots, policy=policy)
