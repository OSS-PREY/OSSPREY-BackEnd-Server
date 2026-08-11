"""Evidence quality, from what the stress run actually produced.

Eight real projects went through both endpoints; these cover the four defects
that showed up in the output.
"""
from app.pain_points import _reading, _trend_line, to_bullets


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
