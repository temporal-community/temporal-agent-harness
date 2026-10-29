"""Packaged browser UI assets for Temporal Agent Harness."""

from pathlib import Path


def packaged_ui_dist() -> Path | None:
    """Return the packaged Vite dist directory when it is available."""

    dist = Path(__file__).with_name("dist")
    if (dist / "index.html").is_file():
        return dist
    return None
