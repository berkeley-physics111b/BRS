import time
import pyvisa


class TempMonitor:
    def __init__(self, ammeter='USB0::0x05E6::0x2110::1373999::INSTR', voltmeter='USB0::0x05E6::0x2110::1415286::INSTR'):
        rm = pyvisa.ResourceManager()
        self.ammeter = rm.open_resource(ammeter)
        self.voltmeter = rm.open_resource(voltmeter)
        self.temp_history = []
        self.temp_spike = False

    def measure_temperature(self):
        alpha = 3.9e-3  # for copper
        R_20 = 22.4  # ohms of magnet coil at room temperature (20 C)
        current = self.measure_current()
        R_obs = self.measure_voltage() / current
        ratio = R_obs / R_20
        # print('DEBUG: ' + str(ratio))
        T = (ratio - 1 + 20 * alpha) / alpha
        if current < 0.01: #account for 0 current case
            T = 20
        self.temp_history.append(T)
        self.check_if_temp_spike()
        return T  # in degrees C

    def measure_voltage(self):
        meter = self.voltmeter
        meter.write("*RST")
        meter.write("FUNCtion 'VOLTage[:DC]'")
        meter.write(":ROUT:CLOS")
        output = float(meter.query(":READ?"))
        return output  # in volts
    
    def measure_current(self):
        meter = self.ammeter
        meter.write("*RST")
        meter.write("FUNCtion 'CURRent[:DC]'")
        meter.write(":ROUT:CLOS")
        output = float(meter.query(":READ?"))
        return output  # in amps
    
    def check_if_temp_spike(self):
        SPIKE_CHECK_VAL = 5
        if (len(self.temp_history) > 1) and (self.temp_history[-1] - self.temp_history[-2] > SPIKE_CHECK_VAL):
            self.temp_spike = True
        else:
            self.temp_spike = False

if __name__ == "__main__":
    monitor = TempMonitor()
    while True:
        # print(monitor.measure_voltage())
        # print(monitor.measure_current())
        print('Temp:', monitor.measure_temperature(), 'C; Current:', monitor.measure_current(), 'A')
        time.sleep(10)