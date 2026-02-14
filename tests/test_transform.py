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


class TestSoilMoistureTransform:
    """Full sweep of our soil moisture use case.

    Config: raw_min=1.0, raw_max=3.3, invert=True
    Sensor reads 3.3V dry, ~1.0V wet. Transform normalizes to [0, 1]
    with invert so 0.0 = bone dry, 1.0 = fully wet.

    raw_voltage → expected_moisture
    """

    # (raw_voltage, expected_output)
    # Below raw_min: clipped to 1.0 → normalized 0.0 → inverted 1.0
    # Above raw_max: clipped to 3.3 → normalized 1.0 → inverted 0.0
    # In range: normalized then inverted
    EXPECTED = [
        # --- Below range (clipped to 1.0 → moisture 1.0) ---
        (0.0, 1.0000),
        (0.1, 1.0000),
        (0.2, 1.0000),
        (0.3, 1.0000),
        (0.4, 1.0000),
        (0.5, 1.0000),
        (0.6, 1.0000),
        (0.7, 1.0000),
        (0.8, 1.0000),
        (0.9, 1.0000),
        # --- At and above raw_min (active range) ---
        (1.0, 1.0000),  # fully wet
        (1.1, 0.9565),
        (1.2, 0.9130),
        (1.3, 0.8696),
        (1.4, 0.8261),
        (1.5, 0.7826),
        (1.6, 0.7391),
        (1.7, 0.6957),
        (1.8, 0.6522),
        (1.9, 0.6087),
        (2.0, 0.5652),
        (2.1, 0.5217),
        (2.2, 0.4783),
        (2.3, 0.4348),
        (2.4, 0.3913),
        (2.5, 0.3478),
        (2.6, 0.3043),
        (2.7, 0.2609),
        (2.8, 0.2174),
        (2.9, 0.1739),
        (3.0, 0.1304),
        (3.1, 0.0870),
        (3.2, 0.0435),
        (3.3, 0.0000),  # bone dry
        # --- Above range (clipped to 3.3 → moisture 0.0) ---
        (3.4, 0.0000),
        (3.5, 0.0000),
        (3.6, 0.0000),
        (3.7, 0.0000),
        (3.8, 0.0000),
        (3.9, 0.0000),
        (4.0, 0.0000),
    ]

    RAW_MIN = 1.0
    RAW_MAX = 3.3

    @pytest.mark.parametrize("raw_voltage,expected", EXPECTED)
    def test_soil_moisture(self, raw_voltage, expected):
        result = transform_value(
            raw_voltage,
            raw_min=self.RAW_MIN,
            raw_max=self.RAW_MAX,
            invert=True,
        )
        assert result == pytest.approx(expected, abs=0.0001)


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

    def test_transform_applies_per_channel(self):
        from sensors.ads1115_sensor import ADS1115ADC
        sensor = ADS1115ADC(
            channels=[0, 1],
            transforms={"0": {"raw_min": 1.0, "raw_max": 3.3, "invert": True}},
        )
        raw = (3.3, 2.0)
        result = sensor.transform(raw)
        assert result[0] == pytest.approx(0.0)  # 3.3 inverted → 0.0
        assert result[1] == 2.0                  # no transform on ch 1

    def test_transform_identity_without_config(self):
        from sensors.ads1115_sensor import ADS1115ADC
        sensor = ADS1115ADC(channels=[0, 1])
        raw = (1.5, 2.5)
        assert sensor.transform(raw) == raw


class TestSensorBaseTransform:
    """Test that the base Sensor.transform() is identity."""

    def test_base_transform_is_identity(self):
        from sensors.base import Sensor

        class DummySensor(Sensor):
            def init(self): pass
            def read(self): return (1.0, 2.0)
            def get_names(self): return ("a", "b")
            def get_units(self): return ("v", "v")

        s = DummySensor()
        assert s.transform((1.0, 2.0)) == (1.0, 2.0)
