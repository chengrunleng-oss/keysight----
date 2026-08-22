from .configs import ext_hardware
from .controller import N5171B

# ip_address = ext_hardware["Keysight N5171B"]["ip_address"]
with N5171B() as instrument:

    idn = instrument.connect()
    print(f"Connected to: {idn}")

    print(f"Frequency: {instrument.output.get_frequency_mhz()} MHz")
    print(f"Power: {instrument.output.get_power_dbm()} dBm")
