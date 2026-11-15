from pathlib import Path

from adef_operate.skills_check import validate_all


def test_all_skills_valid():
    assert validate_all(Path(__file__).parent.parent / "skills") == []
