from noveltrans.gui.widgets import format_duration


class TestFormatDuration:
    def test_unset_is_blank(self):
        assert format_duration(0) == ""
        assert format_duration(-1) == ""
        assert format_duration(0.4) == ""  # rounds to 0

    def test_seconds(self):
        assert format_duration(42) == "42s"
        assert format_duration(59.6) == "1m00s"  # rounds up past a minute

    def test_minutes(self):
        assert format_duration(65) == "1m05s"
        assert format_duration(104) == "1m44s"

    def test_hours(self):
        assert format_duration(3725) == "1h02m"
