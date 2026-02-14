"""Tests for sensor value transforms."""

import pytest

from sensors.base import transform_value


class TestTransformValue:
    """Tests for the transform_value function."""

    def test_no_transform(self):
        assert transform_value(2.5) == 2.5

    def test_min_clip_clamps_up(self):
        assert transform_value(0.5, min_clip=1.0) == 1.0

    def test_min_clip_no_effect(self):
        assert transform_value(2.0, min_clip=1.0) == 2.0

    def test_max_clip_clamps_down(self):
        assert transform_value(3.5, max_clip=3.3) == 3.3

    def test_max_clip_no_effect(self):
        assert transform_value(2.0, max_clip=3.3) == 2.0

    def test_both_clips_passthrough(self):
        assert transform_value(2.0, min_clip=1.0, max_clip=3.3) == 2.0

    def test_both_clips_below(self):
        assert transform_value(0.5, min_clip=1.0, max_clip=3.3) == 1.0

    def test_both_clips_above(self):
        assert transform_value(4.0, min_clip=1.0, max_clip=3.3) == 3.3

    def test_invert_at_min(self):
        assert transform_value(1.0, min_clip=1.0, max_clip=3.0, invert=True) == 3.0

    def test_invert_at_max(self):
        assert transform_value(3.0, min_clip=1.0, max_clip=3.0, invert=True) == 1.0

    def test_invert_mid_value(self):
        assert transform_value(1.5, min_clip=1.0, max_clip=3.0, invert=True) == 2.5

    def test_invert_clips_then_inverts(self):
        # 0.5 -> clip to 1.0 -> invert: 1.0 + 3.0 - 1.0 = 3.0
        assert transform_value(0.5, min_clip=1.0, max_clip=3.0, invert=True) == 3.0

    def test_invert_above_range(self):
        # 4.0 -> clip to 3.0 -> invert: 1.0 + 3.0 - 3.0 = 1.0
        assert transform_value(4.0, min_clip=1.0, max_clip=3.0, invert=True) == 1.0

    def test_invert_requires_min_clip(self):
        with pytest.raises(ValueError, match="invert requires both"):
            transform_value(1.0, max_clip=3.0, invert=True)

    def test_invert_requires_max_clip(self):
        with pytest.raises(ValueError, match="invert requires both"):
            transform_value(1.0, min_clip=1.0, invert=True)

    def test_invert_requires_both_clips(self):
        with pytest.raises(ValueError, match="invert requires both"):
            transform_value(1.0, invert=True)


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
            ADS1115ADC(channels=[0, 1], transforms={"2": {"invert": False}})

    def test_invert_without_clips_raises(self):
        from sensors.ads1115_sensor import ADS1115ADC
        with pytest.raises(ValueError, match="invert requires"):
            ADS1115ADC(transforms={"0": {"invert": True, "min_clip": 1.0}})

    def test_valid_transforms_accepted(self):
        from sensors.ads1115_sensor import ADS1115ADC
        sensor = ADS1115ADC(transforms={
            "0": {"min_clip": 1.0, "max_clip": 3.3, "invert": True},
            "2": {"min_clip": 0.0, "max_clip": 3.3},
        })
        assert 0 in sensor._transforms
        assert 2 in sensor._transforms
        assert 1 not in sensor._transforms

    def test_no_transforms_default(self):
        from sensors.ads1115_sensor import ADS1115ADC
        sensor = ADS1115ADC()
        assert sensor._transforms == {}
