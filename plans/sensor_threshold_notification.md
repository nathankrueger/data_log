# Sensor Threshold Alerts with Hysteresis + Email Notifications

## Context

The user wants to be notified when sensor readings cross configurable thresholds - first use case: soil moisture dropping too low. The system needs hysteresis to prevent notification spam when values oscillate near a boundary. Only one callback per threshold crossing. Recovery notifications ("back to normal") are also sent.

**Notification approach:** Gmail SMTP (built-in `smtplib`, zero new dependencies) for email. Uses a Gmail "app password" - a 16-character code generated in Google Account settings (Security > 2-Step Verification > App passwords). This is the standard, well-established way to send email from Python scripts.

**Where checking happens:** Gateway-side only. The gateway is the only device with internet access, and all sensor data (both remote LoRa and local) flows through `SensorDataCollector.add_readings()`.

## Files to Create

### 1. `sensors/thresholds.py` - Threshold engine (core logic)

Threshold data structures and hysteresis state machine, in the `sensors` package so it's reusable by any sensor implementation.

- `ThresholdConfig` dataclass: `low: float | None`, `high: float | None`, `hysteresis: float = 0.0`
- `AlertState` enum: `NORMAL`, `ALERT_LOW`, `ALERT_HIGH`
- `ThresholdAlert` dataclass: `sensor_id`, `new_state`, `old_state`, `value`, `threshold_value`, `units`, `timestamp`
- `ThresholdMonitor` class:
  - `configure(sensor_id, ThresholdConfig)` - register thresholds for a sensor
  - `check(sensor_id, value, units, timestamp) -> ThresholdAlert | None` - check one reading
  - `_next_state(current, value, config)` - pure static method implementing the state machine

**Hysteresis state machine:**
```
NORMAL -> ALERT_HIGH:  value >= high
NORMAL -> ALERT_LOW:   value <= low
ALERT_HIGH -> NORMAL:  value <= (high - hysteresis)
ALERT_LOW  -> NORMAL:  value >= (low + hysteresis)
ALERT_HIGH -> ALERT_LOW:  value <= low   (direct crossover)
ALERT_LOW  -> ALERT_HIGH: value >= high  (direct crossover)
```

Example (soil moisture, low=1.5V, hysteresis=0.3V):
| Reading | State Before | State After | Alert? |
|---------|-------------|-------------|--------|
| 2.0V | NORMAL | NORMAL | No |
| 1.5V | NORMAL | ALERT_LOW | "LOW ALERT" fired |
| 1.6V | ALERT_LOW | ALERT_LOW | No (in dead band: < 1.8V) |
| 1.8V | ALERT_LOW | NORMAL | "RECOVERED" fired |

### 2. `utils/notifications.py` - Email notification

- `EmailNotifier`: Gmail SMTP with STARTTLS + app password
- Uses only Python stdlib (`smtplib`, `email.mime`). No new dependencies.

### 3. `gateway/alerts.py` - Alert manager

- `AlertManager`: owns `ThresholdMonitor` + `EmailNotifier`
  - `check_readings(node_id, readings)` - called from `add_readings()`, checks all readings, dispatches notifications on state transitions
  - Per-sensor cooldown to prevent re-alerting (configurable `cooldown_sec`)
  - Formats human-readable email subjects/bodies
- `build_alert_manager(config)` - factory that builds everything from gateway config JSON

### 4. `tests/test_thresholds.py` - Threshold state machine tests

Thorough tests for the hysteresis engine: basic threshold crossing, dead band behavior, recovery, direct crossover, multiple independent sensors, None values.

### 5. `tests/test_alerts.py` - Alert manager tests

Tests with mock notifiers: alert firing, cooldown suppression, disabled mode, notifier failure isolation.

## Files to Modify

### 6. `gateway/sensor_collection.py` - Hook in alert checking

- Add optional `alert_manager` param to `SensorDataCollector.__init__()`
- In `add_readings()`, call `alert_manager.check_readings(node_id, readings)` before queuing dashboard post
- Wrapped in try/except so alert failures never block dashboard posting

### 7. `gateway/server.py` - Wire up on startup

- Call `build_alert_manager(config.get("alerts", {}))` during gateway init
- Pass the resulting `AlertManager` (or None) to `SensorDataCollector`

### 8. `config/gateway_config.json.example` - Add alerts config section

```json
"alerts": {
    "enabled": true,
    "thresholds": {
        "patio_ads1115adc_a0": {
            "low": 1.5,
            "hysteresis": 0.3
        }
    },
    "cooldown_sec": 300,
    "email": {
        "enabled": true,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "username": "myemail@gmail.com",
        "app_password": "xxxx xxxx xxxx xxxx",
        "recipients": ["alerts@example.com"]
    }
}
```

Threshold keys must match `make_sensor_id()` output format: `{node_id}_{sensor_class}_{reading_name}` (all lowercase, spaces/hyphens become underscores).

## Key Existing Code to Reuse

- `utils/protocol.py` - `SensorReading` dataclass, `make_sensor_id()` for building sensor IDs
- `gateway/sensor_collection.py` - `SensorDataCollector.add_readings()` is the integration point (all readings flow through here)
- `gateway/server.py` - Gateway startup where config is loaded and components are wired together
- `sensors/base.py` - `Sensor` ABC (thresholds module lives alongside this in the `sensors` package)

## Implementation Order

1. `sensors/thresholds.py` + `tests/test_thresholds.py` (pure logic, test first)
2. `utils/notifications.py` (standalone, no integration needed)
3. `gateway/alerts.py` + `tests/test_alerts.py` (wires 1+2 together)
4. `gateway/sensor_collection.py` + `gateway/server.py` (integration)
5. `config/gateway_config.json.example` (documentation)

## Verification

- Run `pytest tests/test_thresholds.py tests/test_alerts.py -v` on target device
- To test email end-to-end: configure a real Gmail app password in gateway config, set a threshold on a local sensor, and verify the email arrives
- To verify hysteresis: watch logs for "Dispatching alert" messages and confirm only one fires per crossing
