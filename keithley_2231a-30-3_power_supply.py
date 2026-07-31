import pyvisa
import time
import logging

consoleHandler = logging.StreamHandler()
logger = logging.getLogger("keithley2231a")
logger.addHandler(consoleHandler)
logger.setLevel(logging.DEBUG)


class Keithley2231A(object):
    """
    Driver for the Keithley (Tektronix) 2231A-30-3 triple-channel DC power supply.

    Unlike some other bench supplies (e.g. Siglent SPD3303X), the 2231A-30-3 does
    NOT take the channel name as part of every SCPI command. Instead you select a
    channel with INSTrument:SELect (or INSTrument:NSELect), and all subsequent
    VOLTage/CURRent/MEASure/CHANnel:OUTPut commands act on that "currently
    selected" channel. This driver hides that by having each Channel object
    select itself before issuing a command.

    CH1 and CH2 are the two 0-30 V / 3 A adjustable channels.
    CH3 is the fixed 5 V / 3 A channel (voltage/current are not adjustable,
    only its output can be switched on/off), matching the front panel.

    Reference: Model 2231A-30-3 Reference Manual (077-1004-01), Section 5,
    SCPI command reference.
    """

    KNOWN_MODELS = ["2231A-30-3"]

    @classmethod
    def usb_device(cls, visa_rscr: str = None):
        return USBDevice(visa_rscr)

    class Channel(object):
        def __init__(self, chan_no: int, dev):
            self._chan_no = chan_no
            self._name = f"CH{chan_no}"
            self._dev = dev

        def _select(self):
            # Make this channel the "currently selected" channel before any
            # per-channel command, per INSTrument:SELect / INSTrument:NSELect.
            self._dev.write(f"INST:NSEL {self._chan_no}")

        def set_output(self, status: bool):
            self._select()
            self._dev.write(f"CHAN:OUTP {'ON' if status else 'OFF'}")

        def get_output(self):
            self._select()
            return self._dev.query("CHAN:OUTP?")

        def measure_voltage(self):
            self._select()
            return self._dev.query("MEAS:VOLT?")

        def measure_current(self):
            self._select()
            return self._dev.query("MEAS:CURR?")

        def measure_power(self):
            self._select()
            return self._dev.query("MEAS:POW?")

    class ControlledChannel(Channel):
        """CH1 / CH2 - adjustable 0-30 V, 0-3 A channels."""

        def set_voltage(self, voltage: float):
            self._select()
            self._dev.write(f"VOLT {voltage:.3f}")

        def set_current(self, current: float):
            self._select()
            self._dev.write(f"CURR {current:.3f}")

        def get_voltage(self):
            self._select()
            return self._dev.query("VOLT?")

        def get_current(self):
            self._select()
            return self._dev.query("CURR?")

    def all_outputs(self, status: bool):
        """OUTPut[:STATe][:ALL] - switches all three channels together."""
        self.write(f"OUTP {'ON' if status else 'OFF'}")

    def __enter__(self):
        try:
            dsc = self.query("*IDN?")
        except pyvisa.errors.VisaIOError:
            self._inst.close()
            raise
        # Keithley 2231A-30-3 *IDN? format:
        # "Keithley Instruments, 2231A-30-3, <serial>, <firmware>"
        identity_items = [item.strip() for item in dsc.split(",")]
        if len(identity_items) != 4:
            raise Exception(f"Unexpected *IDN? response: {dsc!r}")
        mnf, model, serial, firmware = identity_items
        logger.debug(f"Discovered {model} by {mnf} (SN {serial}, FW {firmware})")
        if model not in self.KNOWN_MODELS:
            raise Exception(f"Device {model} not supported")

        # Switch the instrument into remote mode, per Section 5 of the manual.
        self.write("SYST:REM")

        self.CH1 = Keithley2231A.ControlledChannel(1, self)
        self.CH2 = Keithley2231A.ControlledChannel(2, self)
        self.CH3 = Keithley2231A.Channel(3, self)
        return self

    def __exit__(self, *args):
        try:
            self.write("SYST:LOC")
        finally:
            self._inst.close()


class USBDevice(Keithley2231A):
    def __init__(self, visa_rscr: str = None):
        self._visa_rscr = visa_rscr

    def __enter__(self):
        rm = pyvisa.ResourceManager()
        if self._visa_rscr is None:
            logger.debug("Trying to auto-detect the 2231A-30-3")
            self._visa_rscr = self._auto_detect(rm)
            if self._visa_rscr is None:
                raise Exception("No device found")

        self._inst = rm.open_resource(self._visa_rscr)
        # The 2231A-001 USB adapter exposes a TTL/serial interface; SCPI
        # traffic is newline terminated, and the fixed baud rate is 9600.
        self._inst.write_termination = "\n"
        self._inst.read_termination = "\n"
        if hasattr(self._inst, "baud_rate"):
            self._inst.baud_rate = 9600
        return super().__enter__()

    def _auto_detect(self, rm):
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

    def write(self, cmd: str):
        self._inst.write(cmd)
        time.sleep(0.1)

    def query(self, cmd: str):
        self.write(cmd)
        rep = self._inst.read()
        time.sleep(0.1)
        return rep