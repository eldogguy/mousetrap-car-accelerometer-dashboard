"""Battery charge status (BQ25185) and level (voltage-divider ADC).

STAT1/STAT2 decode is exactly TI's BQ25185 datasheet Table 6-2 (SLUSF65B --
"Status Pins State Table"), confirmed against the schematic
(ACCELEROMETER V2.1.sch) rather than guessed at:

    STAT1 HIGH, STAT2 HIGH -> charge complete / sleep / charge disabled
    STAT1 HIGH, STAT2 LOW  -> charging in progress (incl. auto-recharge)
    STAT1 LOW,  STAT2 HIGH -> recoverable fault (VIN OVP, TS hot/cold,
                              thermal shutdown, output short)
    STAT1 LOW,  STAT2 LOW  -> non-recoverable/latched fault (ILIM/ISET
                              short, battery overcurrent, safety timer
                              expired)

Per the datasheet, with no battery connected at all the charger toggles
STAT2 rapidly (charging/discharging the BAT-pin capacitor) while STAT1
stays stable -- so a STAT2 reading that won't settle between calls is
itself diagnostic of "no battery," not a real fault.

Both STAT pins are open-drain, pulled up 1k to the ESP32's own +3.3V rail
(see config.py) with no inverting/level-shift stage, so no polarity
correction is needed here.

read_voltage()/read_percent() use BATTERY_LEVEL_PIN, the midpoint of a
1M/1M divider off the raw battery rail (BATTERY_DIVIDER_RATIO = 2.0).
Percent uses a simple linear map over a single-cell LiPo's usable range
(3.3V empty - 4.2V full) -- not a proper discharge-curve lookup, so treat
it as a rough indicator, not a precise fuel gauge.

IMPORTANT caveat, confirmed on real hardware: this reads the terminal
voltage right at the battery, which is only a meaningful state-of-charge
proxy when the battery is at rest (read_charge_state() == CHARGE_DONE).
While actively charging (CHARGING), the BQ25185 -- a *linear* charger --
regulates voltage directly at that same terminal, and the charge current
flowing through the battery's own internal resistance adds a real IR-drop
term on top of the true state-of-charge voltage
(V_terminal = V_true_SOC + I_charge * R_internal). Plugging in a partially
discharged cell can push the terminal voltage toward the ~4.2V regulation
point almost immediately, well before the battery has actually reached
that state of charge -- read_percent() will read deceptively high in
exactly that situation. This isn't a bug or noise to filter out; it's a
real electrical property of the measurement point, and fixing it properly
would need a dedicated fuel-gauge IC (e.g. a coulomb counter like a
MAX17048), not a firmware change. Treat percent readings taken while
CHARGING as optimistic/unreliable, and trust readings taken at rest
(CHARGE_DONE) instead.
"""

import machine

from config import (
    BQ25185_STAT1_PIN,
    BQ25185_STAT2_PIN,
    BATTERY_LEVEL_PIN,
    BATTERY_DIVIDER_RATIO,
    BATTERY_LOW_VOLTAGE,
    BATTERY_CRITICAL_VOLTAGE,
)

_stat1 = machine.Pin(BQ25185_STAT1_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
_stat2 = machine.Pin(BQ25185_STAT2_PIN, machine.Pin.IN, machine.Pin.PULL_UP)

_adc = machine.ADC(machine.Pin(BATTERY_LEVEL_PIN))
_adc.atten(machine.ADC.ATTN_11DB)  # full ~0-3.3V range; VBAT/2 can reach ~2.1V

CHARGE_DONE = "done"
CHARGING = "charging"
FAULT_RECOVERABLE = "fault_recoverable"
FAULT_LATCHED = "fault_latched"

_EMPTY_V = 3.3  # single-cell LiPo, rough "empty" cutoff
_FULL_V = 4.2   # single-cell LiPo, fully charged


def read_charge_state():
    """One of CHARGE_DONE/CHARGING/FAULT_RECOVERABLE/FAULT_LATCHED -- see
    module docstring for the full STAT1/STAT2 truth table."""
    s1 = _stat1.value()
    s2 = _stat2.value()
    if s1 and s2:
        return CHARGE_DONE
    if s1 and not s2:
        return CHARGING
    if not s1 and s2:
        return FAULT_RECOVERABLE
    return FAULT_LATCHED


def read_voltage():
    """Battery voltage in volts, via the BATTERY_LEVEL_PIN divider."""
    return _adc.read_uv() / 1_000_000 * BATTERY_DIVIDER_RATIO


def read_percent():
    """Rough battery level 0-100, linearly mapped between _EMPTY_V and
    _FULL_V -- see module docstring caveat about LiPo discharge curves."""
    v = read_voltage()
    pct = (v - _EMPTY_V) / (_FULL_V - _EMPTY_V) * 100.0
    return max(0, min(100, round(pct)))


def is_low():
    """True below BATTERY_LOW_VOLTAGE -- informational warning threshold."""
    return read_voltage() < BATTERY_LOW_VOLTAGE


def is_critical():
    """True below BATTERY_CRITICAL_VOLTAGE -- main.py refuses to arm below
    this (see config.py's comment for why: a real observed hang under WiFi
    load on a weak battery, not just "runs out mid-recording")."""
    return read_voltage() < BATTERY_CRITICAL_VOLTAGE
