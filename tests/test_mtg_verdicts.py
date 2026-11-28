import pytest

from tagplatform.services.generation.open.mtg.verdicts import (
    VERDICT_FLOORS, VerdictTarget, verdict_pass)

def test_floors_values():
    assert VERDICT_FLOORS == {"relevance": 3, "semantics": 3, "value": 2}

@pytest.mark.parametrize("rel,sem,val,expected", [
    (4, 3, 3.0, True), (3, 3, 2.0, True),
    (2, 4, 3.0, False), (4, 2, 3.0, False), (4, 3, 1.99, False),
    (None, 3, 3.0, None), (4, None, 3.0, None), (4, 3, None, None),
])
def test_verdict_pass(rel, sem, val, expected):
    assert verdict_pass(rel, sem, val) == expected

def test_verdict_target_fields():
    t = VerdictTarget(key="t0", tag="逃离安排", level="film",
                      allowed_scenes=frozenset({"scene_001"}),
                      tag_kind="content", has_desc=True)
    assert t.key == "t0" and t.level == "film"
