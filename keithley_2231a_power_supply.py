import pyvisa
import time
import logging

logger = logging.getLogger("keithley2231a")
if not logger.hasHandlers():
    logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.DEBUG)


class Keithley2231AException(Exception):
    """
    Raised for any Keithley2231A driver-level error (device not found,
    unexpected/unsupported *IDN? response, VISA I/O failure, invalid
    channel/quantity/setpoint, etc).

    Wraps the original exception (if any) so callers can still inspect the
    root cause via the `original_error` attribute, while getting a single,
    consistent driver-specific exception type to catch.
    """

    def __init__(self, message: str, original_error: Exception = None):
        self.original_error = original_error
        if original_error is not None:
            message = f"{message} (caused by {type(original_error).__name__}: {original_error})"
        super().__init__(message)


class Keithley2231A(object):
    """
    Driver for the Keithley (Tektronix) 2231A-30-3 triple-channel DC power supply.

    Unlike some other bench supplies (e.g. Siglent SPD3303X), the 2231A-30-3 does
    NOT take the channel name as part of every SCPI command. Instead you select a
    channel with INSTrument:SELect (or INSTrument:NSELect), and all subsequent
    VOLTage/CURRent/MEASure/CHANnel:OUTPut commands act on that "currently
    selected" channel. This driver hides that: every channel_* method selects
    the requested channel before issuing its SCPI command.

    CH1 and CH2 are the two 0-30 V / 3 A adjustable channels.
    CH3 is 0-5 V / 3 A. Channel numbering matches front panel.

    Usage:
        with Keithley2231A("ASRL5::INSTR") as supply:
            supply.channel_set(channel=1, value=5.0, quantity="voltage")
            supply.channel_set(channel=1, value=0.5, quantity="current")
            supply.channel_enable(channel=1)
            print(supply.channel_measure(channel=1, quantity="voltage"))
            supply.channel_disable(channel=1)

    If visa_resource is omitted, the driver probes every VISA resource and
    picks the first one that answers *IDN? as a known model.

    All driver-level errors are raised as Keithley2231AException, which wraps
    the original underlying exception (e.g. a pyvisa.errors.VisaIOError) in
    its `original_error` attribute.

    Reference: Model 2231A-30-3 Reference Manual (077-1004-01), Section 5,
    SCPI command reference.
    """

    KNOWN_MODELS = ["2231A-30-3"]

    LARGE_RANGE_CHANNELS = (1, 2) # CH1 / CH2: 0-30V, 0-3A
    SMALL_RANGE_CHANNELS = (3,) # CH3: 0-5 V, 0-3A
    ALL_CHANNELS = LARGE_RANGE_CHANNELS + SMALL_RANGE_CHANNELS

    # Allowed setpoint ranges, matching
    # LARGE_RANGE_CHANNELS / SMALL_RANGE_CHANNELS: CH1/CH2 are 0-30 V, 0-3 A;
    # CH3 is 0-5 V, 0-3 A. Keyed by quantity, values are (min, max) inclusive.
    LARGE_RANGE_LIMITS = {"voltage": (0.0, 30.0), "current": (0.0, 3.0)}
    SMALL_RANGE_LIMITS = {"voltage": (0.0, 5.0), "current": (0.0, 3.0)}

    QUANTITY_COMMANDS = {
        "voltage": "VOLT",
        "current": "CURR",
    }
    MEASURE_COMMANDS = {
        "voltage": "MEAS:VOLT?",
        "current": "MEAS:CURR?",
        "power": "MEAS:POW?",
    }

    FINE_TUNE_A = 200.0
    FINE_TUNE_B = 0.0

    def __init__(self, visa_resource: str = None, baud_rate: int = 9600):
        """
        visa_resource: VISA resource string for the 2231A-001 USB/serial
            adapter (e.g. "ASRL5::INSTR"). If None, the driver will try to
            auto-detect the instrument by probing *IDN? on every resource
            VISA can see.
        baud_rate: fixed at 9600 by the 2231A-001 adapter; exposed here in
            case a different interface is ever used.
        """
        self._visa_resource = visa_resource
        self._baud_rate = baud_rate
        self._rm = None
        self._inst = None

    # -- connection lifecycle -------------------------------------------------

    def __enter__(self):
        try:
            self._rm = pyvisa.ResourceManager()
        except Exception as e:
            raise Keithley2231AException("Failed to create VISA ResourceManager", e) from e

        if self._visa_resource is None:
            logger.debug("No VISA resource given, trying to auto-detect the 2231A-30-3")
            self._visa_resource = self._auto_detect(self._rm)
            if self._visa_resource is None:
                raise Keithley2231AException("No device found")

        try:
            self._inst = self._rm.open_resource(self._visa_resource)
        except Exception as e:
            raise Keithley2231AException(
                f"Failed to open VISA resource {self._visa_resource!r}", e
            ) from e

        # SCPI traffic over the 2231A-001 adapter is newline terminated at a
        # fixed baud rate.
        self._inst.write_termination = "\n"
        self._inst.read_termination = "\n"
        if hasattr(self._inst, "baud_rate"):
            self._inst.baud_rate = self._baud_rate

        try:
            dsc = self.query("*IDN?")
        except pyvisa.errors.VisaIOError as e:
            self._inst.close()
            raise Keithley2231AException("Failed to query *IDN? from device", e) from e

        # Keithley 2231A-30-3 *IDN? format:
        # "Keithley Instruments, 2231A-30-3, <serial>, <firmware>"
        identity_items = [item.strip() for item in dsc.split(",")]
        if len(identity_items) != 4:
            raise Keithley2231AException(f"Unexpected *IDN? response: {dsc!r}")
        mnf, model, serial, firmware = identity_items
        logger.debug(f"Discovered {model} by {mnf} (SN {serial}, FW {firmware})")
        if model not in self.KNOWN_MODELS:
            raise Keithley2231AException(f"Device {model} not supported")

        # Switch the instrument into remote mode, per Section 5 of the manual.
        self.write("SYST:REM")
        return self

    def __exit__(self, *args):
        try:
            self.write("SYST:LOC")
        finally:
            if self._inst is not None:
                self._inst.close()

    @staticmethod
    def _auto_detect(rm):
        # There's no fixed VISA resource-name substring for the 2231A-001
        # adapter (it enumerates as a generic USB-serial/ASRL device), so
        # probe each available resource and check its *IDN? response instead
        # of matching on the resource string.
        for res_str in rm.list_resources():
            try:
                inst = rm.open_resource(res_str)
                inst.write_termination = "\n"
                inst.read_termination = "\n"
                if hasattr(inst, "baud_rate"):
                    inst.baud_rate = 9600
                inst.write("*IDN?")
                time.sleep(0.1)
                idn = inst.read()
                inst.close()
                if any(model in idn for model in Keithley2231A.KNOWN_MODELS):
                    return res_str
            except (pyvisa.errors.VisaIOError, Exception):
                continue
        return None

    # -- raw SCPI I/O -----------------------------------------------------

    def write(self, cmd: str):
        try:
            self._inst.write(cmd)
        except Exception as e:
            raise Keithley2231AException(f"Failed to write SCPI command {cmd!r}", e) from e
        time.sleep(0.1)

    def query(self, cmd: str):
        self.write(cmd)
        try:
            rep = self._inst.read()
        except Exception as e:
            raise Keithley2231AException(
                f"Failed to read response for SCPI command {cmd!r}", e
            ) from e
        time.sleep(0.1)
        return rep

    # -- channel validation -------------------------------------------------

    def _select_channel(self, channel: int):
        if channel not in self.ALL_CHANNELS:
            raise Keithley2231AException(
                f"channel must be one of {self.ALL_CHANNELS}, got {channel!r}"
            )
        self.write(f"INST:NSEL {channel}")

    def _check_adjustable(self, channel: int, value: float, quantity: str):
        """
        Validate that `value` is within the allowed range for `quantity`
        (voltage/current) on `channel`. CH1/CH2 use LARGE_RANGE_LIMITS
        (0-30 V, 0-3 A) and CH3 uses SMALL_RANGE_LIMITS (0-5 V, 0-3 A).
        """
        if channel in self.LARGE_RANGE_CHANNELS:
            limits = self.LARGE_RANGE_LIMITS
        elif channel in self.SMALL_RANGE_CHANNELS:
            limits = self.SMALL_RANGE_LIMITS
        else:
            raise Keithley2231AException(
                f"channel must be one of {self.ALL_CHANNELS}, got {channel!r}"
            )

        if quantity not in limits:
            raise Keithley2231AException(
                f"quantity must be one of {tuple(limits)}, got {quantity!r}"
            )

        low, high = limits[quantity]
        if not (low <= value <= high):
            raise Keithley2231AException(
                f"{quantity} setpoint {value} is out of range for CH{channel}; "
                f"must be between {low} and {high}"
            )

    @staticmethod
    def _check_quantity(quantity: str, valid: dict):
        quantity = quantity.lower()
        if quantity not in valid:
            raise Keithley2231AException(f"quantity must be one of {tuple(valid)}, got {quantity!r}")
        return quantity

    # -- per-channel control --------------------------------------------------

    def channel_enable(self, channel: int):
        """Turn a single channel's output ON."""
        self._select_channel(channel)
        self.write("CHAN:OUTP ON")

    def channel_disable(self, channel: int):
        """Turn a single channel's output OFF."""
        self._select_channel(channel)
        self.write("CHAN:OUTP OFF")

    def channel_output_enabled(self, channel: int) -> bool:
        """Return True if the channel's output is currently ON."""
        self._select_channel(channel)
        resp = self.query("CHAN:OUTP?").strip()
        return resp in ("1", "ON")

    def channel_set(self, channel: int, value: float, quantity: str = "voltage"):
        """
        Set the voltage or current setpoint on a channel.

        channel: 1, 2, or 3
        value: setpoint, in volts or amps depending on `quantity`. Must fall
            within the channel's allowed range (0-30 V / 0-3 A for CH1/CH2,
            0-5 V / 0-3 A for CH3).
        quantity: "voltage" or "current"
        """
        quantity = self._check_quantity(quantity, self.QUANTITY_COMMANDS)
        self._check_adjustable(channel, value, quantity)
        self._select_channel(channel)
        self.write(f"{self.QUANTITY_COMMANDS[quantity]} {value:.3f}")

    def channel_get_setpoint(self, channel: int, quantity: str = "voltage") -> float:
        """Read back the programmed voltage or current setpoint."""
        quantity = self._check_quantity(quantity, self.QUANTITY_COMMANDS)
        self._select_channel(channel)
        resp = self.query(f"{self.QUANTITY_COMMANDS[quantity]}?")
        try:
            return float(resp)
        except ValueError as e:
            raise Keithley2231AException(
                f"Could not parse setpoint response {resp!r} as float", e
            ) from e

    def channel_measure(self, channel: int, quantity: str = "voltage") -> float:
        """
        Measure actual voltage, current, or power on any channel (CH1/CH2/CH3).
        quantity: "voltage", "current", or "power"
        """
        quantity = self._check_quantity(quantity, self.MEASURE_COMMANDS)
        self._select_channel(channel)
        resp = self.query(self.MEASURE_COMMANDS[quantity])
        try:
            return float(resp)
        except ValueError as e:
            raise Keithley2231AException(
                f"Could not parse measurement response {resp!r} as float", e
            ) from e

    # -- all-channel control --------------------------------------------------

    def all_outputs(self, status: bool):
        """OUTPut[:STATe][:ALL] - switches all three channels together."""
        self.write(f"OUTP {'ON' if status else 'OFF'}")

    # -- fine control via external board --------------------------------------
    
    def fine_control_enable(self):
        """Enables all channels at once for BRS fine current control using external board."""
        self.channel_enable(channel=1)
        self.channel_enable(channel=2)
        self.channel_enable(channel=3)

    def fine_control_disable(self):
        """Disables all channels at once for BRS fine current control using external board."""
        self.channel_disable(channel=1)
        self.channel_disable(channel=2)
        self.channel_disable(channel=3)

    def voltage_to_current(self, voltage):
        return round(self.FINE_TUNE_A * voltage + self.FINE_TUNE_B, 6) # assuming resolution of ~uA post-board

    def current_to_voltage(self, current):
        return round((current - self.FINE_TUNE_B) / self.FINE_TUNE_A, 2) # resolution of 10 mV on Keithley

    def fine_control_set(self, value):
        """Using external board, increases precision of power supply output by combining output
        from two channels. Value is assumed to be in A."""
        coarse_offset = self.current_to_voltage(round(value, 2))
        fine_offset = round(value - round(value,2), 6) # value after 10 mA (rounded to deal with floating point)
        fine_offset_volt = self.current_to_voltage(fine_offset)
        self.channel_set(channel=1, value=coarse_offset, quantity="voltage")
        self.channel_set(channel=2, value=5, quantity="voltage")
        self.channel_set(channel=3, value=fine_offset_volt, quantity="voltage")

    def fine_control_measure(self):
        """Read version of fine_control_set. Returns value in A."""
        coarse_offset = self.channel_measure(channel=1, quantity="voltage")
        fine_offset_volt = self.channel_measure(channel=3, quantity="voltage")
        fine_offset = self.voltage_to_current(fine_offset_volt)
        return coarse_offset + fine_offset

if __name__ == "__main__":
    # Simple hardware test:
    #   1. Connect and print *IDN?
    #   2. Program CH1 to a low, safe voltage with a tight current limit
    #   3. Enable CH1's output
    #   4. Read back the setpoint and the actual measured voltage
    #   5. Disable the output again
    #   6. Tests fine current control.
    #
    # Set VISA_RESOURCE as None to let the driver auto-detect the instrument.
    VISA_RESOURCE = "ASRL8::INSTR"

    TEST_CHANNEL = 1
    TEST_VOLTAGE = 1.0     # volts - low enough to be safe with nothing/anything connected
    TEST_CURRENT_LIMIT = 0.005  # amps

    with Keithley2231A(VISA_RESOURCE) as supply:
        idn = supply.query("*IDN?")
        print(f"*IDN?: {idn}")

        supply.channel_set(channel=TEST_CHANNEL, value=TEST_VOLTAGE, quantity="voltage")
        supply.channel_set(channel=TEST_CHANNEL, value=TEST_CURRENT_LIMIT, quantity="current")
        supply.channel_enable(channel=TEST_CHANNEL)

        time.sleep(5)  # let the output settle / user visually check

        setpoint = supply.channel_get_setpoint(channel=TEST_CHANNEL, quantity="voltage")
        measured = supply.channel_measure(channel=TEST_CHANNEL, quantity="voltage")
        enabled = supply.channel_output_enabled(channel=TEST_CHANNEL)

        print(f"CH{TEST_CHANNEL} setpoint : {setpoint:.3f} V")
        print(f"CH{TEST_CHANNEL} measured : {measured:.3f} V")
        print(f"CH{TEST_CHANNEL} output on: {enabled}")

        assert abs(setpoint - TEST_VOLTAGE) < 0.01, (
            f"Setpoint readback {setpoint} V does not match commanded {TEST_VOLTAGE} V"
        )
        assert enabled, f"CH{TEST_CHANNEL} output did not report ON after channel_enable()"
        # Measured voltage should be close to the setpoint as long as the load
        # (or open-circuit condition) doesn't trip the current limit.
        assert abs(measured - TEST_VOLTAGE) < 0.2, (
            f"Measured voltage {measured} V is far from setpoint {TEST_VOLTAGE} V "
            f"(is the current limit tripping, or is there a heavy load on CH{TEST_CHANNEL}?)"
        )

        supply.channel_disable(channel=TEST_CHANNEL)
        print("Test passed.")
        print("Testing fine current control.")

        supply.fine_control_enable()
        supply.fine_control_set(value=0.0111)
        time.sleep(3)
        value_a = supply.fine_control_measure()
        supply.fine_control_set(value=0.0112)
        time.sleep(3)
        value_b = supply.fine_control_measure()
        print(f"resolution estimate: {round(value_b - value_a, 6)}")
        supply.fine_control_disable()
        print("Test finished.")