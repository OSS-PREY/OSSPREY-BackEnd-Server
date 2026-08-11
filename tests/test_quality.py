"""Evidence quality, from what the stress run actually produced.

Eight real projects went through both endpoints; these cover the four defects
that showed up in the output.
"""
from app.pain_points import _reading, _trend_line, build_evidence, to_bullets


class TestReading:
    """kubernetes' busiest developer holds 4% of the work, and the model
    reported "high concentration of work among a few accounts". A bare
    percentage does not tell it which direction is bad."""

    def test_names_a_low_share_as_normal(self):
        assert 'normal, not a concern' in _reading(0.04, 0.30, 0.10)

    def test_names_a_high_share_as_high(self):
        assert _reading(0.62, 0.30, 0.10).endswith('high')

    def test_hedges_in_between(self):
        assert 'moderate' in _reading(0.20, 0.30, 0.10)


class TestTrend:
    """redis came back with "significant increase in active developers (up 225%
    overall)" beside pain points about decline. Any mature project is up
    against its first month."""

    def test_reports_the_recent_move_as_well_as_lifetime(self):
        # The collapse must fall INSIDE the last twelve months; a decline that
        # finished earlier and has been flat since is correctly silent.
        rising_then_collapsing = (
            [{'month': m, 'value': 10 + m} for m in range(20)]
            + [{'month': m, 'value': 30 - (m - 20) * 2} for m in range(20, 32)])
        line = _trend_line('devs', rising_then_collapsing)

        assert 'overall' in line
        assert 'over the last 12 months' in line
        assert 'down' in line.split('overall')[1]

    def test_stays_quiet_when_recent_change_is_small(self):
        flat = [{'month': m, 'value': 20} for m in range(30)]

        assert 'last 12 months' not in (_trend_line('devs', flat) or '')

    def test_still_names_the_peak(self):
        arc = [{'month': m, 'value': v} for m, v in enumerate([1, 9, 4, 3])]

        assert 'peak 9 at m1' in _trend_line('devs', arc)


class TestBullets:
    def test_strips_the_internal_reading_tag(self):
        # gem5 quoted the scaffolding straight into a bullet.
        out = to_bullets('- Silent committers: 475 of 556 wrote nothing -- high')

        assert out == ['- Silent committers: 475 of 556 wrote nothing']

    def test_strips_the_normal_tag_too(self):
        assert to_bullets('- Busiest developer at 4% -- normal, not a concern') \
            == ['- Busiest developer at 4%']

    def test_leaves_a_real_dash_alone(self):
        out = to_bullets('- Contributors fell 31 -> 14 -- and none joined')

        assert out == ['- Contributors fell 31 -> 14 -- and none joined']


class TestPartialFinalMonth:
    """A scrape usually catches its last month partway through. django ends on
    6 developers against a median of 30 for the year before, all flat, and that
    was read as "sharp decline in active developers".

    The figure is real data and is never removed -- it is stated on its own,
    and the trend is measured without it.
    """

    FLAT_THEN_PART = ([{'month': m, 'value': 30} for m in range(24)]
                      + [{'month': 24, 'value': 6}])

    def test_keeps_the_month_but_states_it_apart(self):
        line = _trend_line('devs', self.FLAT_THEN_PART)

        # Still visible...
        assert 'm24 is still in progress and shows 6 so far' in line
        # ...but not sitting at the end of the run of numbers, where it reads
        # as the trend's conclusion whatever label it carries.
        assert 'm24=6' not in line

    def test_does_not_call_a_flat_year_a_collapse(self):
        assert 'down' not in _trend_line('devs', self.FLAT_THEN_PART)

    def test_a_real_fall_still_shows(self):
        # A genuine decline over the year is not hidden by this.
        falling = [{'month': m, 'value': max(2, 40 - m * 2)} for m in range(20)]
        line = _trend_line('devs', falling)

        assert 'down' in line
        assert 'still in progress' not in line

    def test_needs_enough_history_to_judge(self):
        # Three months is not enough to call the last one partial.
        short = [{'month': 0, 'value': 30}, {'month': 1, 'value': 30}, {'month': 2, 'value': 4}]

        assert 'still in progress' not in _trend_line('devs', short)


class TestNoDiscussionData:
    """"Every committer was silent" is true by construction when the issue
    scrape returned nothing; kubernetes and django both reported 100% silent
    committers for that reason alone."""

    NONE = {'span': 'all', 'social': {
        'series': {'participants': [{'month': m, 'value': 0} for m in range(6)]},
        'silent_developers': {'count': 4760, 'total': 4760}}}

    SOME = {'span': 'all', 'social': {
        'series': {'participants': [{'month': m, 'value': 20} for m in range(6)]},
        'silent_developers': {'count': 560, 'total': 828}}}

    def test_suppresses_the_vacuous_finding(self):
        evidence = build_evidence(self.NONE, 'kubernetes')

        assert 'silent committers' not in evidence
        assert 'no discussion' in evidence

    def test_keeps_it_when_there_is_discussion_to_be_absent_from(self):
        assert 'silent committers: 560' in build_evidence(self.SOME, 'redis')
