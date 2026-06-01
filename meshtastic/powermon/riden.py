"""Classes for logging power consumption of Meshtastic devices."""

import logging
import math
import time
from datetime import datetime
from typing import Any, Final, cast

import riden

from .constants import MILLIAMPS_PER_AMP, SECONDS_PER_HOUR
from .power_supply import PowerError, PowerSupply

INVALID_POWER_ON_VOLTAGE_ERROR: Final[str] = (
    "Voltage must be set to a positive value before powerOn()."
)
DEFAULT_RIDEN_BAUDRATE: Final[int] = 115200
DEFAULT_RIDEN_ADDRESS: Final[int] = 1

Riden = cast(type[Any], riden.Riden)  # type: ignore[attr-defined]


class RidenPowerSupply(PowerSupply):
    """Interface for talking to Riden programmable bench-top power supplies.
    Only RD6006 tested but others should be similar.
    """

    def __init__(self, portName: str = "/dev/ttyUSB0") -> None:
        """Initialize the RidenPowerSupply object.

        Parameters
        ----------
        portName : str, optional
            The serial port path of the power supply. Defaults to ``"/dev/ttyUSB0"``.
        """
        self.r = r = Riden(
            port=portName,
            baudrate=DEFAULT_RIDEN_BAUDRATE,
            address=DEFAULT_RIDEN_ADDRESS,
        )
        logging.info(
            "Connected to Riden power supply: model %s, sn %s, firmware %s. Date/time updated.",
            r.type,
            r.sn,
            r.fw,
        )
        r.set_date_time(datetime.now())
        # Keep base init after port open so timing/voltage state is available.
        super().__init__()
        self.prevWattHour = self._get_raw_watt_hour()
        # COMPAT_STABLE_SHIM: retained for callers that inspect legacy running sample state.
        self.nowWattHour = self.prevWattHour
        self.prevPowerTime = time.monotonic()

    def setMaxCurrent(self, i: float) -> None:
        """Set the maximum current the supply will provide."""
        self.r.set_i_set(i)

    def powerOn(self) -> None:
        """Power on the supply, with reasonable defaults for meshtastic devices."""
        if self.v <= 0:
            raise PowerError(INVALID_POWER_ON_VOLTAGE_ERROR)
        self.r.set_v_set(
            self.v
        )  # my WM1110 devboard header is directly connected to the 3.3V rail
        self.r.set_output(True)

    def getAverageCurrentMA(self) -> float:
        """Return average current of last measurement in mA since last call to this method.

        Returns
        -------
        float
            Average current in milliamperes, or ``math.nan`` for invalid windows.
        """
        now = time.monotonic()
        nowWattHour = self._get_raw_watt_hour()
        self.nowWattHour = nowWattHour
        elapsed_s = now - self.prevPowerTime
        if elapsed_s <= 0:
            # Consume the window to avoid stale deltas on subsequent reads.
            self.prevPowerTime = now
            self.prevWattHour = nowWattHour
            return math.nan
        delta_watt_hour = nowWattHour - self.prevWattHour
        # Intentional: consume this measurement window even when voltage <= 0 to avoid a
        # large energy spike after voltage recovers.
        self.prevPowerTime = now
        self.prevWattHour = nowWattHour
        if delta_watt_hour < 0:
            # Counter reset/rollover or transient read glitch; resync baseline.
            return math.nan
        watts = (delta_watt_hour / elapsed_s) * SECONDS_PER_HOUR
        if self.v <= 0:
            return math.nan
        return (watts / self.v) * MILLIAMPS_PER_AMP

    # COMPAT_STABLE_SHIM: snake_case alias retained for scripting/tooling callers.
    def get_average_current_mA(self) -> float:  # pylint: disable=invalid-name
        """Compatibility alias for `getAverageCurrentMA`.

        Returns
        -------
        float
            Average current in milliamperes.
        """
        return self.getAverageCurrentMA()

    def _get_raw_watt_hour(self) -> float:
        """Get the current watt-hour reading."""
        self.r.update()
        return float(self.r.wh)

    # COMPAT_STABLE_SHIM: historical private helper alias retained for external integrations.
    def _getRawWattHour(self) -> float:  # pylint: disable=invalid-name
        """Compatibility alias for _get_raw_watt_hour()."""
        return self._get_raw_watt_hour()
