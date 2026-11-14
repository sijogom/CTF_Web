"""개인전과 고정 명단 팀전이 공유할 기존 점수 정책의 순수 회귀."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from app.utils import scoring


START = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
END = START + timedelta(hours=2)


def config(*, kind="static", initial=500, decay=10, minimum=50):
    return SimpleNamespace(
        scoring_type=kind, initial_value=initial, decay=decay, minimum_value=minimum
    )


class CompetitionTeamScoringTests(unittest.TestCase):
    def test_existing_dynamic_formula_and_bounds_do_not_change(self):
        self.assertEqual(scoring.calc_dynamic_value(500, 10, 50, 0), 500)
        self.assertEqual(scoring.calc_dynamic_value(500, 10, 50, 1), 496)
        self.assertEqual(scoring.calc_dynamic_value(500, 10, 50, 2), 482)
        self.assertEqual(scoring.calc_dynamic_value(500, 10, 50, 10), 50)
        self.assertEqual(scoring.calc_dynamic_value(500, 10, 50, 100), 50)
        self.assertEqual(scoring.calc_dynamic_value(500, 0, 50, 1), 50)

    def test_freeze_start_and_end_are_strict_boundaries(self):
        cutoff = END - timedelta(minutes=30)
        for instant, frozen, expected_cutoff in (
            (cutoff - timedelta(microseconds=1), False, None),
            (cutoff, False, None),
            (cutoff + timedelta(microseconds=1), True, cutoff),
            (END - timedelta(microseconds=1), True, cutoff),
            (END, False, None),
            (END + timedelta(microseconds=1), False, None),
        ):
            with self.subTest(instant=instant):
                self.assertEqual(
                    scoring.competition_freeze_window(END, 30, instant),
                    (frozen, expected_cutoff),
                )

    def test_disabled_freeze_has_no_cutoff(self):
        self.assertEqual(
            scoring.competition_freeze_window(END, 0, END - timedelta(seconds=1)),
            (False, None),
        )

    def test_freeze_does_not_introduce_new_start_time_clamping(self):
        self.assertEqual(
            scoring.competition_freeze_window(END, 180, START),
            (True, END - timedelta(minutes=180)),
        )

    def test_challenge_values_share_formula_and_use_provided_ledger_count(self):
        configs = {1: config(), 2: config(kind="dynamic"), 3: config(initial=300)}
        self.assertEqual(
            scoring.competition_challenge_values(configs, {1: 20, 2: 1}),
            {1: 500, 2: 496, 3: 300},
        )
        # 팀원 수나 개인 풀이 수를 함수 안에서 팀 풀이 수에 더하지 않는다.
        self.assertEqual(scoring.competition_challenge_values({2: configs[2]}, {2: 2}), {2: 482})

    def test_scoreboard_keeps_zero_solve_subjects_and_sequential_ranks(self):
        subjects = [("a", "Alpha"), ("b", "Beta"), ("z", "Zero")]
        solves = [("a", 1, START), ("b", 1, START + timedelta(seconds=1))]
        entries = scoring.competition_scoreboard_entries(subjects, solves, {1: 500})
        self.assertEqual(entries, [
            {"rank": 1, "subject_id": "a", "name": "Alpha", "score": 500, "solves": 1, "first_bloods": 1},
            {"rank": 2, "subject_id": "b", "name": "Beta", "score": 500, "solves": 1, "first_bloods": 0},
            {"rank": 3, "subject_id": "z", "name": "Zero", "score": 0, "solves": 0, "first_bloods": 0},
        ])

    def test_scoreboard_tie_is_name_only_not_time_solve_count_or_first_blood(self):
        subjects = [("a", "Zulu"), ("b", "Alpha")]
        solves = [("a", 1, START), ("b", 2, START + timedelta(seconds=1)), ("b", 3, START + timedelta(seconds=2))]
        entries = scoring.competition_scoreboard_entries(subjects, solves, {1: 500, 2: 250, 3: 250})
        self.assertEqual([entry["subject_id"] for entry in entries], ["b", "a"])
        self.assertEqual([entry["rank"] for entry in entries], [1, 2])
        self.assertEqual([entry["first_bloods"] for entry in entries], [2, 1])

    def test_first_blood_counts_every_subject_at_the_exact_minimum(self):
        subjects = [("a", "Alpha"), ("b", "Beta"), ("c", "Later")]
        solves = [("a", 1, START), ("b", 1, START), ("c", 1, START + timedelta(microseconds=1))]
        entries = scoring.competition_scoreboard_entries(subjects, solves, {1: 500})
        self.assertEqual({entry["subject_id"]: entry["first_bloods"] for entry in entries}, {"a": 1, "b": 1, "c": 0})

    def test_unknown_challenge_config_keeps_existing_zero_value_behavior(self):
        entries = scoring.competition_scoreboard_entries([("a", "Alpha")], [("a", 99, START)], {})
        self.assertEqual(entries[0]["score"], 0)
        self.assertEqual(entries[0]["solves"], 1)

    def test_graph_uses_current_values_for_all_historical_points(self):
        solves = [("a", 1, START + timedelta(minutes=1)), ("a", 2, START + timedelta(minutes=2)), ("b", 1, START + timedelta(minutes=3))]
        series = scoring.competition_scoreboard_series(START, solves, {1: 482, 2: 300}, {"a": "Alpha", "b": "Beta"})
        self.assertEqual(series, [
            {"subject_id": "a", "name": "Alpha", "data": [
                {"t": START.isoformat(), "score": 0},
                {"t": (START + timedelta(minutes=1)).isoformat(), "score": 482},
                {"t": (START + timedelta(minutes=2)).isoformat(), "score": 782},
            ]},
            {"subject_id": "b", "name": "Beta", "data": [
                {"t": START.isoformat(), "score": 0},
                {"t": (START + timedelta(minutes=3)).isoformat(), "score": 482},
            ]},
        ])

    def test_graph_ties_keep_first_event_order_not_scoreboard_name_order(self):
        solves = [("a", 1, START), ("b", 1, START + timedelta(seconds=1))]
        series = scoring.competition_scoreboard_series(START, solves, {1: 500}, {"a": "Zulu", "b": "Alpha"})
        self.assertEqual([row["subject_id"] for row in series], ["a", "b"])

    def test_graph_has_only_solvers_and_default_top_ten(self):
        solves = [(str(index), 1, START + timedelta(seconds=index)) for index in range(12)]
        names = {str(index): str(index) for index in range(13)}
        series = scoring.competition_scoreboard_series(START, solves, {1: 500}, names)
        self.assertEqual(len(series), 10)
        self.assertEqual([row["subject_id"] for row in series], [str(index) for index in range(10)])
        self.assertNotIn("12", [row["subject_id"] for row in series])


if __name__ == "__main__":
    unittest.main()
