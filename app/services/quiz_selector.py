"""
Quiz Question Selector — difficulty-based random selection.

Selects N questions from a quiz with a target difficulty distribution.
Fills deficits from adjacent difficulty levels when a bucket is short.
"""

import random
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Adjacency map: which difficulties to borrow from when a bucket is short
_ADJACENCY: Dict[str, List[str]] = {
    "easy":   ["medium"],
    "medium": ["easy", "hard"],
    "hard":   ["medium", "expert"],
    "expert": ["hard"],
}

_DEFAULT_DISTRIBUTION = {"easy": 0.50, "medium": 0.30, "hard": 0.15, "expert": 0.05}


def select_questions(
    questions: List[Any],
    count: int,
    distribution: Dict[str, float] | None = None,
) -> List[Any]:
    """Select `count` questions with a target difficulty distribution.

    Args:
        questions: All QuizQuestion objects in the quiz.
        count: How many questions to select (e.g. 20).
        distribution: Difficulty → ratio (e.g. {"easy": 0.50, ...}).
                      Must sum to ~1.0. Defaults to 50/30/15/5.

    Returns:
        Shuffled list of selected questions.
    """
    if not questions:
        return []

    if count >= len(questions):
        result = list(questions)
        random.shuffle(result)
        return result

    dist = distribution or _DEFAULT_DISTRIBUTION

    # Group by difficulty
    buckets: Dict[str, List[Any]] = {}
    for q in questions:
        diff = getattr(q, "difficulty", None) or "easy"
        buckets.setdefault(diff, []).append(q)

    # Shuffle each bucket so sampling is random
    for bucket in buckets.values():
        random.shuffle(bucket)

    # Calculate target counts per difficulty
    targets: Dict[str, int] = {}
    remaining = count
    sorted_diffs = sorted(dist.keys(), key=lambda d: dist[d], reverse=True)

    for i, diff in enumerate(sorted_diffs):
        if i == len(sorted_diffs) - 1:
            targets[diff] = remaining
        else:
            t = round(dist[diff] * count)
            targets[diff] = t
            remaining -= t

    # Phase 1: Pick from each bucket up to target
    selected: List[Any] = []
    used: Dict[str, int] = {}  # how many picked from each bucket

    for diff in sorted_diffs:
        available = buckets.get(diff, [])
        pick = min(targets[diff], len(available))
        selected.extend(available[:pick])
        used[diff] = pick

    # Phase 2: Fill deficits from adjacent buckets
    for diff in sorted_diffs:
        deficit = targets[diff] - used[diff]
        if deficit <= 0:
            continue

        for neighbor in _ADJACENCY.get(diff, []):
            if deficit <= 0:
                break
            neighbor_bucket = buckets.get(neighbor, [])
            neighbor_used = used.get(neighbor, 0)
            neighbor_available = neighbor_bucket[neighbor_used:]
            fill = min(deficit, len(neighbor_available))
            if fill > 0:
                selected.extend(neighbor_available[:fill])
                used[neighbor] = neighbor_used + fill
                deficit -= fill

    # Phase 3: If still short (rare), fill from any remaining questions
    if len(selected) < count:
        selected_ids = {id(q) for q in selected}
        pool = [q for q in questions if id(q) not in selected_ids]
        random.shuffle(pool)
        fill = min(count - len(selected), len(pool))
        selected.extend(pool[:fill])

    random.shuffle(selected)

    logger.info(
        f"Question selection: {len(selected)}/{count} from {len(questions)} total | "
        f"targets={targets} | "
        f"actual={_count_by_difficulty(selected)}"
    )

    return selected


def _count_by_difficulty(questions: List[Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for q in questions:
        diff = getattr(q, "difficulty", None) or "easy"
        counts[diff] = counts.get(diff, 0) + 1
    return counts
