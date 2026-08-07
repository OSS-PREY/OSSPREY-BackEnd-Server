import math

import pytest

from app.pipeline.calibration import (
    BETA,
    P_CLIP,
    TEMPERATURE,
    activity_index,
    calibrate,
    month_features,
)


def tech(*pairs):
    """[[developer, filetype, commits], ...]"""
    return [[dev, "py", n] for dev, n in pairs]


def social(*pairs):
    return [[sender, "someone", n] for sender, n in pairs]


def test_month_features_counts_commits_and_committers():
    f = month_features(tech(("ann", 6), ("bo", 2), ("ann", 2)), social(("ann", "3")))
    assert f["commits"] == 10
    # ann and bo -> 2 committers, 10 commits
    assert f["commits_per_committer"] == 5
    # Mongo stores weights as strings; they must still count.
    assert f["messages"] == 3
    assert f["messages_per_sender"] == 3


def test_month_features_survives_junk_rows():
    f = month_features([["a", "py"], None, "nope", ["a", "py", "x"]], None)
    assert f["commits"] == 0
    assert f["commits_per_committer"] == 0
    assert f["messages_per_sender"] == 0


def test_saturated_forecast_stops_being_flat():
    """The whole point: 1.0 every month must not render as one flat line."""
    forecast = {m: 1.0 for m in range(12)}
    # Activity ramps down: a project quietly losing its contributors.
    t = {m: tech(*[(f"dev{i}", 10) for i in range(12 - m)]) for m in range(12)}
    s = {m: social(*[(f"dev{i}", 5) for i in range(12 - m)]) for m in range(12)}

    out = calibrate(forecast, t, s)

    assert len(set(round(v, 3) for v in out.values())) > 1, "still flat"
    # And it must trend *down*, tracking the decline the model missed.
    assert out[0] > out[11]


def test_rising_activity_reads_higher_than_falling():
    forecast = {m: 1.0 for m in range(10)}
    rising = {m: tech(*[(f"d{i}", 4) for i in range(m + 1)]) for m in range(10)}
    falling = {m: tech(*[(f"d{i}", 4) for i in range(10 - m)]) for m in range(10)}

    up = calibrate(forecast, rising, {})
    down = calibrate(forecast, falling, {})

    assert up[9] > up[0]
    assert down[9] < down[0]


def test_model_still_dominates_a_decisive_prediction():
    """Activity modulates the reading; it must never flip a confident model.

    A confident 1.0 with the worst possible activity must still read higher than
    a confident 0.0 with the best possible activity.
    """
    assert BETA < _logit(P_CLIP) / TEMPERATURE

    busy = {0: tech(("a", 1)), 1: tech(*[(f"d{i}", 99) for i in range(30)])}
    quiet = {0: tech(*[(f"d{i}", 99) for i in range(30)]), 1: tech(("a", 1))}

    high = calibrate({0: 1.0, 1: 1.0}, quiet, {})   # confident yes, activity worst at m1
    low = calibrate({0: 0.0, 1: 0.0}, busy, {})     # confident no, activity best at m1

    assert min(high.values()) > max(low.values())


def _logit(p):
    return math.log(p / (1 - p))


def test_equal_activity_preserves_model_ordering():
    flat = {m: tech(("a", 5)) for m in range(4)}
    out = calibrate({0: 0.2, 1: 0.4, 2: 0.6, 3: 0.8}, flat, flat)
    vals = [out[m] for m in range(4)]
    assert vals == sorted(vals)


def test_dead_project_stays_flat():
    """No commits, no messages, model at 0.0 -- flat is the honest answer."""
    forecast = {m: 0.0 for m in range(6)}
    out = calibrate(forecast, {m: [] for m in range(6)}, {})
    assert len(set(round(v, 6) for v in out.values())) == 1
    # Neutral activity leaves the temperature-scaled floor: sigmoid(-logit(P_CLIP)/T).
    floor = 1 / (1 + math.exp(_logit(P_CLIP) / TEMPERATURE))
    assert out[0] == pytest.approx(floor)
    assert max(out.values()) < 0.2


def test_output_never_saturates_and_stays_in_range():
    out = calibrate({0: 1.0, 1: 0.0, 2: 0.5}, {}, {})
    for v in out.values():
        assert 0.0 < v < 1.0


def test_non_month_keys_are_ignored():
    """The orchestrator injects project_name/project_id beside the month keys."""
    t = {"0": tech(("a", 1)), "1": tech(("a", 9)), "project_name": "x", "project_id": "y"}
    out = calibrate({0: 1.0, 1: 1.0}, t, {})
    assert set(out) == {0, 1}
    assert out[1] > out[0]


def test_input_is_not_mutated():
    forecast = {0: 1.0, 1: 1.0}
    before = dict(forecast)
    calibrate(forecast, {0: tech(("a", 1)), 1: tech(("a", 5))}, {})
    assert forecast == before


def test_empty_and_single_month_are_safe():
    assert calibrate({}, {}, {}) == {}
    one = calibrate({0: 1.0}, {0: tech(("a", 3))}, {})
    assert set(one) == {0}
    assert 0.0 < one[0] < 1.0


def test_activity_index_is_neutral_without_signal():
    idx = activity_index({m: month_features([], []) for m in range(4)}, [0, 1, 2, 3])
    assert set(idx.values()) == {0.5}
