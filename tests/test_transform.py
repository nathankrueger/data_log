"""Tests for sensor value transforms."""

import pytest

from sensors.base import transform_value


class TestTransformValue:
    """Tests for the transform_value function."""

    def test_at_min(self):
        assert transform_value(1.0, raw_min=1.0, raw_max=3.3) == 0.0

    def test_at_max(self):
        assert transform_value(3.3, raw_min=1.0, raw_max=3.3) == 1.0

    def test_midpoint(self):
        result = transform_value(2.15, raw_min=1.0, raw_max=3.3)
        assert result == pytest.approx(0.5, abs=0.01)

    def test_clips_below_min(self):
        assert transform_value(0.5, raw_min=1.0, raw_max=3.3) == 0.0

    def test_clips_above_max(self):
        assert transform_value(4.0, raw_min=1.0, raw_max=3.3) == 1.0

    def test_invert_at_min(self):
        assert transform_value(1.0, raw_min=1.0, raw_max=3.3, invert=True) == 1.0

    def test_invert_at_max(self):
        assert transform_value(3.3, raw_min=1.0, raw_max=3.3, invert=True) == 0.0

    def test_invert_midpoint(self):
        result = transform_value(2.15, raw_min=1.0, raw_max=3.3, invert=True)
        assert result == pytest.approx(0.5, abs=0.01)

    def test_invert_clips_below(self):
        assert transform_value(0.5, raw_min=1.0, raw_max=3.3, invert=True) == 1.0

    def test_invert_clips_above(self):
        assert transform_value(4.0, raw_min=1.0, raw_max=3.3, invert=True) == 0.0

    def test_zero_based_range(self):
        assert transform_value(5.0, raw_min=0.0, raw_max=10.0) == 0.5


class TestADS1115Channels:
    """Constructor validation tests for channels (no hardware needed)."""

    def test_default_all_channels(self):
        from sensors.ads1115_sensor import ADS1115ADC, ADS1115Channel
        sensor = ADS1115ADC()
        assert sensor._active_channels == list(ADS1115Channel)
        assert sensor._names == ("A0", "A1", "A2", "A3")
        assert len(sensor._units) == 4

    def test_subset_channels(self):
        from sensors.ads1115_sensor import ADS1115ADC, ADS1115Channel
        sensor = ADS1115ADC(channels=[0, 1])
        assert sensor._active_channels == [ADS1115Channel.A0, ADS1115Channel.A1]
        assert sensor._names == ("A0", "A1")
        assert len(sensor._units) == 2

    def test_custom_names_match_channels(self):
        from sensors.ads1115_sensor import ADS1115ADC
        sensor = ADS1115ADC(
            channels=[0, 1],
            names=["Soil 1", "Soil 2"],
        )
        assert sensor._names == ("Soil 1", "Soil 2")

    def test_names_length_mismatch_raises(self):
        from sensors.ads1115_sensor import ADS1115ADC
        with pytest.raises(ValueError, match="names length"):
            ADS1115ADC(channels=[0, 1], names=["A", "B", "C"])

    def test_units_length_mismatch_raises(self):
        from sensors.ads1115_sensor import ADS1115ADC
        with pytest.raises(ValueError, match="units length"):
            ADS1115ADC(channels=[0, 1], units=["v"])

    def test_invalid_channel_number_raises(self):
        from sensors.ads1115_sensor import ADS1115ADC
        with pytest.raises(ValueError):
            ADS1115ADC(channels=[0, 5])


class TestADS1115Transforms:
    """Constructor validation tests for transforms (no hardware needed)."""

    def test_transform_on_inactive_channel_raises(self):
        from sensors.ads1115_sensor import ADS1115ADC
        with pytest.raises(ValueError, match="not in active channels"):
            ADS1115ADC(channels=[0, 1], transforms={
                "2": {"raw_min": 1.0, "raw_max": 3.3},
            })

    def test_missing_raw_min_raises(self):
        from sensors.ads1115_sensor import ADS1115ADC
        with pytest.raises(ValueError, match="requires both raw_min and raw_max"):
            ADS1115ADC(transforms={"0": {"raw_max": 3.3}})

    def test_missing_raw_max_raises(self):
        from sensors.ads1115_sensor import ADS1115ADC
        with pytest.raises(ValueError, match="requires both raw_min and raw_max"):
            ADS1115ADC(transforms={"0": {"raw_min": 1.0}})

    def test_raw_min_gte_raw_max_raises(self):
        from sensors.ads1115_sensor import ADS1115ADC
        with pytest.raises(ValueError, match="raw_min.*must be less than"):
            ADS1115ADC(transforms={"0": {"raw_min": 3.3, "raw_max": 1.0}})

    def test_valid_transforms_accepted(self):
        from sensors.ads1115_sensor import ADS1115ADC
        sensor = ADS1115ADC(transforms={
            "0": {"raw_min": 1.0, "raw_max": 3.3, "invert": True},
            "2": {"raw_min": 0.0, "raw_max": 3.3},
        })
        assert 0 in sensor._transforms
        assert 2 in sensor._transforms
        assert 1 not in sensor._transforms

    def test_no_transforms_default(self):
        from sensors.ads1115_sensor import ADS1115ADC
        sensor = ADS1115ADC()
        assert sensor._transforms == {}
