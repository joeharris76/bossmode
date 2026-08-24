from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_uat import EXPECTED_UAT_CHECKS, UATHarness  # noqa: E402


@pytest.mark.uat
def test_automated_uat_end_to_end(tmp_path: Path) -> None:
    harness = UATHarness(tmp_path)
    try:
        success = harness.run_all()
        assert success is True
        assert harness.report.failed == 0
        assert tuple(result.name for result in harness.report.results) == EXPECTED_UAT_CHECKS
    finally:
        harness.cleanup()
