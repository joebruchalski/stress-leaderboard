"""Shared pytest fixtures.

Nothing here touches the user's real ~/.config/stress_analyzer directory —
every test that needs config/DB state builds its own temp paths and, where
relevant, monkeypatches stress_core's module-level path constants so code
under test (including dashboard.py, which reads them indirectly through
stress_core) never reaches for the real files.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def local_tz():
    """A fixed timezone (not the machine's) so tests are deterministic
    regardless of where they run."""
    return ZoneInfo("America/New_York")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR
