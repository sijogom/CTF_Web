"""Dynamic Value 점수 계산 — CTFd 포물선 감소 공식."""

import math
from datetime import datetime, timedelta


def calc_dynamic_value(initial: int, decay: int, minimum: int, solves: int) -> int:
    """풀이 수 기반 동적 점수 계산.

    공식: value = max(minimum, ceil(((minimum - initial) / decay²) * solves² + initial))
    - initial: 초기 점수 (0명 풀이 시)
    - decay: 최저점에 도달하는 풀이 수
    - minimum: 하한 점수
    - solves: 현재 풀이 수
    """
    if solves <= 0:
        return initial
    if decay <= 0:
        return minimum

    value = ((minimum - initial) / (decay ** 2)) * (solves ** 2) + initial
    return max(minimum, math.ceil(value))


def competition_freeze_window(
    end_at: datetime, freeze_minutes: int, now: datetime,
) -> tuple[bool, datetime | None]:
    start = end_at - timedelta(minutes=freeze_minutes) if freeze_minutes else None
    frozen = bool(start and start < now < end_at)
    return frozen, start if frozen else None


def competition_challenge_values(configs, solve_counts) -> dict[int, int]:
    return {
        challenge_id: (
            calc_dynamic_value(config.initial_value, config.decay, config.minimum_value,
                               solve_counts.get(challenge_id, 0))
            if config.scoring_type == "dynamic" else config.initial_value
        )
        for challenge_id, config in configs.items()
    }


def competition_scoreboard_entries(subjects, solves, challenge_values) -> list[dict]:
    first_times = {}
    by_subject = {}
    for subject_id, challenge_id, solved_at in solves:
        by_subject.setdefault(subject_id, []).append((challenge_id, solved_at))
        first_times[challenge_id] = min(first_times.get(challenge_id, solved_at), solved_at)
    entries = []
    for subject_id, name in subjects:
        rows = by_subject.get(subject_id, [])
        entries.append({
            "subject_id": subject_id, "name": name,
            "score": sum(challenge_values.get(challenge_id, 0) for challenge_id, _ in rows),
            "solves": len(rows),
            "first_bloods": sum(solved_at == first_times[challenge_id] for challenge_id, solved_at in rows),
        })
    entries.sort(key=lambda row: (-row["score"], row["name"]))
    return [{"rank": rank, **row} for rank, row in enumerate(entries, 1)]


def competition_scoreboard_series(start_at, solves, challenge_values, names) -> list[dict]:
    running = {}
    data = {}
    # SQL supplies time order; equal timestamps keep its existing relative order.
    for subject_id, challenge_id, solved_at in solves:
        running[subject_id] = running.get(subject_id, 0) + challenge_values.get(challenge_id, 0)
        points = data.setdefault(subject_id, [])
        if not points:
            points.append({"t": start_at.isoformat(), "score": 0})
        points.append({"t": solved_at.isoformat(), "score": running[subject_id]})
    series = [{"subject_id": subject_id, "name": names.get(subject_id, "unknown"), "data": points}
              for subject_id, points in data.items()]
    series.sort(key=lambda row: row["data"][-1]["score"] if row["data"] else 0, reverse=True)
    return series[:10]
