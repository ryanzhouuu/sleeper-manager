from sleeper_manager.decisions.lineup import AssignmentCandidate, maximum_weight_assignment


def test_assignment_uses_dummy_for_negative_scores_and_respects_player_uniqueness() -> None:
    result = maximum_weight_assignment(
        [
            AssignmentCandidate("p1-pg", "p1", 5, ("PG",)),
            AssignmentCandidate("p1-util", "p1", 100, ("UTIL",)),
            AssignmentCandidate("p2", "p2", 4, ("PG",)),
            AssignmentCandidate("negative", "p3", -10, ("UTIL",)),
        ],
        ("PG", "UTIL"),
    )

    assert result.score == 104
    assert [assignment.player_id for assignment in result.assignments] == ["p2", "p1"]


def test_assignment_supports_exact_slots_and_required_alternative_edges() -> None:
    candidates = (
        AssignmentCandidate("p1-g", "p1", 8, ("PG",), eligible_slot_indices=(2,)),
        AssignmentCandidate("p2-util", "p2", 7, ("PG",), eligible_slot_indices=(5,)),
    )

    selected = maximum_weight_assignment(
        candidates,
        ("G", "UTIL"),
        slot_indices=(2, 5),
    )
    alternative = maximum_weight_assignment(
        candidates,
        ("G", "UTIL"),
        slot_indices=(2, 5),
        forbidden_edges=frozenset({(2, "p1-g")}),
        required_edges=frozenset({(5, "p2-util")}),
    )

    assert tuple(item.slot_index for item in selected.assignments) == (2, 5)
    assert tuple(item.player_id for item in selected.assignments) == ("p1", "p2")
    assert tuple(item.player_id for item in alternative.assignments) == (None, "p2")
