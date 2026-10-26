"""Reuse must preserve exhaustive opposing-action comparisons and deterministic policy ties."""

import random
from dataclasses import replace

from sleeper_manager.decisions.completed_scores import (
    CompletedPlacement,
    select_completed_placement,
)
from sleeper_manager.decisions.lineup import SlotAssignment


def _reference(options, tolerance, confidence):
    baseline = next(row for row in options if not row.locks)
    selected = baseline
    for row in options:
        wins = sum(
            a > b + tolerance
            for a, b in zip(row.terminal_scores, baseline.terminal_scores, strict=True)
        )
        if row.locks and wins / len(row.terminal_scores) < confidence:
            continue
        allowed = True
        for lock in row.locks if confidence > 0 else ():
            other = max(
                (
                    candidate
                    for candidate in options
                    if not any(
                        (item.player_id, item.game_id) == (lock.player_id, lock.game_id)
                        for item in candidate.locks
                    )
                ),
                key=lambda candidate: candidate.expected_score,
            )
            wins = sum(
                a > b + tolerance
                for a, b in zip(row.terminal_scores, other.terminal_scores, strict=True)
            )
            if wins / len(row.terminal_scores) < confidence:
                allowed = False
                break
        if not allowed:
            continue
        if row.expected_score > selected.expected_score + tolerance:
            selected = row
        elif (
            row.locks
            and selected.locks
            and abs(row.expected_score - selected.expected_score) <= tolerance
        ):
            verified = bool(row.flexibility and selected.flexibility)
            if (verified and row.flexibility > selected.flexibility) or (
                not (verified and selected.flexibility > row.flexibility)
                and (len(row.moves), tuple((lock.player_id, lock.slot_index) for lock in row.locks))
                < (
                    len(selected.moves),
                    tuple((lock.player_id, lock.slot_index) for lock in selected.locks),
                )
            ):
                selected = row
    return selected


def test_shared_counterfactuals_match_exhaustive_selection() -> None:
    randomizer = random.Random(20261026)
    locks = tuple(SlotAssignment(i, "UTIL", f"done-{i}", f"p{i}", f"g{i}", 30) for i in range(3))
    for _ in range(40):
        options = []
        for mask in range(8):
            scores = tuple(float(randomizer.randrange(-2, 6)) for _ in range(8))
            option = CompletedPlacement(
                tuple(lock for i, lock in enumerate(locks) if mask & (1 << i)),
                (),
                (),
                sum(scores) / len(scores),
                scores,
                frozenset(range(randomizer.randrange(4))),
            )
            options.extend((option, replace(option, locks=tuple(reversed(option.locks)))))
        randomizer.shuffle(options)
        for threshold in (0.0, 0.4, 0.7, 1.0):
            expected = _reference(options, 0.01, threshold)
            actual = select_completed_placement(tuple(options), 0.01, threshold)
            assert actual.selected == expected
            assert actual.alternatives == tuple(options)
