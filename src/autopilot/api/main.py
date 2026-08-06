"""Uvicorn entrypoint for the configured production control plane."""

from autopilot.api.runtime import create_production_app

app = create_production_app()

__all__ = ["app"]
