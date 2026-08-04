from sleeper_manager.domain.eligibility import eligible_for_slot


def test_guard_is_eligible_for_guard_and_utility_slots() -> None:
    assert eligible_for_slot(["PG"], "G")
    assert eligible_for_slot(["PG"], "UTIL")
    assert not eligible_for_slot(["PG"], "F")


def test_exact_position_is_eligible() -> None:
    assert eligible_for_slot(["C", "PF"], "C")
