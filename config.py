import logging
import sys

# Create basic logger
logger = logging.getLogger('power_monitor')
logger.setLevel(logging.INFO)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s : %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
ch.setFormatter(formatter)
logger.addHandler(ch)


# Using a multimeter, measure the voltage of the receptacle where your 9V AC transformer will plug into. Enter the measured value below.
GRID_VOLTAGE = 247.4
# Using a multimeter, measure the output voltage of your AC transformer. Using the value on the label is not ideal and will lead to greater accuracy in the calculations.
AC_TRANSFORMER_OUTPUT_VOLTAGE = 27.71

# InfluxDB Settings
db_settings = {
    'host' : 'localhost',
    'port' : 8086,
    'username' : 'root',
    'password' : 'password',
    'database' : 'power_monitor'
}


# Define Variables
ct0_channel = 0             # 'CT1' House main 100A
ct1_channel = 1             # 'CT2' F1 32A Sockets: kitchen; garden room; outside socket
ct2_channel = 2             # 'CT3' F6 32A Sockets: office; craft room; Lou's room
ct3_channel = 3             # 'CT4' F12 32A Sockets: hall; front room; master bedroom; back bedroom; garage
ct4_channel = 6             # 'CT5' Hot tub
board_voltage_channel =  4  # Board voltage ~3.3V
v_sensor_channel = 5        # 28AC Voltage channel
ct5_channel = 7             # 'CT6' Lights
night_rate = 0.1187         # £ per kWh (as of Dec 2021)
day_rate = 0.2126           # £ per kWh (as of Dec 2021)

# The values from running the software in "phase" mode should go below!
ct_phase_correction = {
    'ct0' : 1.03307871,
    'ct1' : 1.06106079,
    'ct2' : 1.16228187,
    'ct3' : 1.37349277,
    'ct4' : 1.04173608,
    'ct5' : 1.53331616,
}

# AFTER phase correction is completed, these values are used in the final calibration for accuracy. See the documentation for more information.
accuracy_calibration = {
    'ct0' : 0.91265,
    'ct1' : 0.34887,
    'ct2' : 0.34939,
    'ct3' : 0.34545,
    'ct4' : 0.34568,
    'ct5' : 0.35085,
    'AC'  : 3.32114,
}
