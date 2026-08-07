"""Post-hoc calibration of the forecaster's sustainability probabilities.

The transformer saturates. On established projects it returns 1.0 for every month
(hugo 158/158, scikit-learn 200/200, fastapi 93/93), which draws a flat line and
tells a reader nothing -- a project shedding contributors looks identical to one
at its peak. This module leaves the model completely alone. It takes the
probability the model produced and re-expresses it against the project's own
socio-technical activity, using exactly the four features the dashboard already
shows:

    commits, commits per committer, issues/emails, issues/emails per sender

Two things happen, both in log-odds space:

  * temperature -- the model's log-odds are divided by TEMPERATURE, so a
    saturated 1.0 stops being infinitely confident and lands in a readable band.
  * activity -- an index in [0, 1] built from the four features shifts the
    log-odds by at most BETA. A month where the project is busier than its own
    norm reads higher; a month where contributors are drifting away reads lower,
    even when the model insists on 1.0 for both.

The index is relative to the project's own history (a percentile, not an
absolute count), so 20 commits means one thing on a small project and another on
a large one.

This is a presentation-layer recalibration: it makes the panel legible and
directionally honest. TEMPERATURE and BETA are set by judgement, not fitted
against graduation outcomes -- there are no labels here to fit them against. Two
properties are deliberate and covered by tests:

  * BETA < logit(P_CLIP) / TEMPERATURE, so activity modulates the reading but
    can never overrule a decisive model output.
  * with equal activity, the model's ordering of two months is preserved.

Genuinely dead stretches (no commits, no messages, model at 0.0) still come back
flat. That is the correct answer, not a defect: nothing is happening.
"""

import math

# Divides the model's log-odds. Larger = less confident output.
TEMPERATURE = 4.0

# Largest log-odds shift the activity index may apply, in either direction.
# Held below the model's own post-temperature range (logit(0.999)/4 = 1.73) so a
# decisive model output still dominates.
BETA = 1.2

# Clip before the logit so p == 1.0 does not blow up to infinity.
P_CLIP = 0.999

# How much of the index is "where does this month sit in project history" versus
# "which way is the project trending right now".
_LEVEL_WEIGHT = 0.6

_NEUTRAL = 0.5

# The four features the dashboard already shows.
_SIGNALS = ("commits", "commits_per_committer", "messages", "messages_per_sender")


def _sigmoid(z):
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _logit(p):
    p = min(max(float(p), 1.0 - P_CLIP), P_CLIP)
    return math.log(p / (1.0 - p))


def _num(x):
    """Edge weights arrive as int from the forecaster and as str from Mongo."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def month_features(tech_rows, social_rows):
    """The four dashboard features for a single month.

    tech_rows   -- [[developer, filetype, commits], ...]
    social_rows -- [[sender, recipient, messages], ...]
    """
    tech = [r for r in (tech_rows or []) if isinstance(r, (list, tuple)) and len(r) == 3]
    social = [r for r in (social_rows or []) if isinstance(r, (list, tuple)) and len(r) == 3]

    commits = sum(_num(r[2]) for r in tech)
    committers = len({r[0] for r in tech if r[0]})
    messages = sum(_num(r[2]) for r in social)
    senders = len({r[0] for r in social if r[0]})

    return {
        "commits": commits,
        "commits_per_committer": commits / committers if committers else 0.0,
        "messages": messages,
        "messages_per_sender": messages / senders if senders else 0.0,
    }


def _percentiles(values):
    """Rank each value within the series, ties averaged, scaled to [0, 1].

    Percentile rather than z-score: these counts are heavy-tailed, and one
    release-month spike should not flatten every other month against the axis.
    """
    n = len(values)
    if n < 2 or max(values) == min(values):
        return [_NEUTRAL] * n

    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def _smooth3(values):
    """Centred 3-point mean, so the line moves without sawtoothing."""
    n = len(values)
    if n < 3:
        return list(values)
    return [
        sum(values[max(0, i - 1):min(n, i + 2)]) / (min(n, i + 2) - max(0, i - 1))
        for i in range(n)
    ]


def activity_index(features_by_month, months):
    """Combine the four features into one index in [0, 1] per month.

    0.5 is "a typical month for this project"; above is busier than its own norm,
    below is quieter.
    """
    if not months:
        return {}

    # log1p first: the gap between 1 and 10 commits matters more than 501 vs 510.
    logged = {
        s: [math.log1p(max(0.0, features_by_month.get(m, {}).get(s, 0.0))) for m in months]
        for s in _SIGNALS
    }

    # A signal that never moves (a project with no mailing list at all, say)
    # carries no information; drop it rather than pinning it at a flat 0.5.
    live = [s for s in _SIGNALS if max(logged[s]) > min(logged[s])]
    if not live:
        return {m: _NEUTRAL for m in months}

    ranked = {s: _percentiles(logged[s]) for s in live}
    level = [sum(ranked[s][i] for s in live) / len(live) for i in range(len(months))]

    # Momentum: recent activity against the project's whole-history baseline.
    # This is what separates "at 1.0 and thriving" from "at 1.0 while the
    # contributors quietly leave".
    combined = [sum(logged[s][i] for s in live) / len(live) for i in range(len(months))]
    mean = sum(combined) / len(combined)
    sd = math.sqrt(sum((v - mean) ** 2 for v in combined) / len(combined))
    if sd == 0:
        momentum = [_NEUTRAL] * len(months)
    else:
        momentum = [_sigmoid((v - mean) / sd) for v in _smooth3(combined)]

    index = [
        _LEVEL_WEIGHT * level[i] + (1.0 - _LEVEL_WEIGHT) * momentum[i]
        for i in range(len(months))
    ]
    return dict(zip(months, _smooth3(index)))


def _month_key(k):
    """Month dicts are keyed by int here and by str in JSON/Mongo."""
    try:
        return int(k)
    except (TypeError, ValueError):
        return None


def _by_month(rows_by_month):
    """Normalise a {month: rows} mapping to int keys, dropping anything else.

    The orchestrator injects 'project_name'/'project_id' alongside the month
    keys, so non-numeric keys must be skipped rather than parsed.
    """
    out = {}
    for k, v in (rows_by_month or {}).items():
        m = _month_key(k)
        if m is not None and isinstance(v, (list, tuple)):
            out[m] = v
    return out


def calibrate(forecast, tech_months, social_months):
    """Calibrate {month: probability} against the project's own activity.

    tech_months / social_months are {month: rows} as stored in net-vis JSON and
    in the tech_net / social_net Mongo documents; months missing from them score
    as neutral.

    Returns a new {month: probability} with the same keys as ``forecast``. The
    input is never mutated, and a project with no usable activity data comes back
    temperature-scaled only -- never crashed, never empty.
    """
    if not forecast:
        return {}

    tech = _by_month(tech_months)
    social = _by_month(social_months)

    months = sorted(forecast)
    features = {m: month_features(tech.get(m), social.get(m)) for m in months}
    index = activity_index(features, months)

    return {
        m: _sigmoid(
            _logit(forecast[m]) / TEMPERATURE
            + BETA * (2.0 * index.get(m, _NEUTRAL) - 1.0)
        )
        for m in months
    }
