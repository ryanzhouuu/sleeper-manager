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
