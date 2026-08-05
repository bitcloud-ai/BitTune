from autopilot import __version__


def test_package_exposes_semantic_version() -> None:
    assert __version__ == "0.1.0"
