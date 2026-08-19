from collections.abc import Iterable


def weighted_mean(values: Iterable[tuple[float, float]]) -> float:
    records = tuple(values)
    total_weight = sum(weight for _, weight in records)
    if not records or total_weight == 0:
        raise ValueError("Weighted mean requires observations with non-zero total weight")
    return sum(value * weight for value, weight in records) / total_weight
