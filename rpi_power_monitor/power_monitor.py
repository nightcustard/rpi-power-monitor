#!/usr/bin/python
import csv
import logging
import os
import pickle
import sys
import timeit
from datetime import datetime
from math import sqrt
from shutil import copyfile
from socket import AF_INET
from socket import SOCK_DGRAM
from socket import socket
from textwrap import dedent
from time import sleep

import spidev
from prettytable import PrettyTable

import rpi_power_monitor.influx_interface as infl
from rpi_power_monitor.config import ACCURACY_CALIBRATION
from rpi_power_monitor.config import AC_TRANSFORMER_OUTPUT_VOLTAGE
from rpi_power_monitor.config import ADC_CHANNELS
from rpi_power_monitor.config import CT_PHASE_CORRECTION
from rpi_power_monitor.config import GRID_VOLTAGE
from rpi_power_monitor.config import db_settings
from rpi_power_monitor.config import logger
from rpi_power_monitor.plotting import plot_data


class RPiPowerMonitor:
    """ Class to take readings from the MCP3008 and calculate power """
    def __init__(self,
                 spi=None,
                 grid_voltage=GRID_VOLTAGE,
                 ac_transformer_output_voltage=AC_TRANSFORMER_OUTPUT_VOLTAGE,
                 ct_phase_correction=CT_PHASE_CORRECTION,
                 accuracy_calibration=ACCURACY_CALIBRATION,
                 adc_channels=ADC_CHANNELS
                 ):
        self.grid_voltage = grid_voltage
        self.ac_transformer_output_voltage = ac_transformer_output_voltage
        self.ct_phase_correction = ct_phase_correction
        self.accuracy_calibration = accuracy_calibration
        self.adc_channels = adc_channels

        if spi:
            self.spi = spi
        else:
            self.spi = spidev.SpiDev()  # Create SPI
            self.spi.open(0, 0)
            self.spi.max_speed_hz = 1750000  # Changing this value will require you to adjust the phasecal values in config.py.

    def dump_data(self, dump_type, samples):
        """ Writes raw data to a CSV file titled 'data-dump-<current_time>.csv' """
        speed_kHz = self.spi.max_speed_hz / 1000
        now = datetime.now().strftime('%m-%d-%Y-%H-%M')
        filename = f'data-dump-{now}.csv'
        with open(filename, 'w') as f:
            headers = ["Sample#", "ct1", "ct2", "ct3", "ct4", "ct5", "ct6", "voltage"]
            writer = csv.writer(f)
            writer.writerow(headers)
            # samples contains lists for each data sample.
            for i in range(0, len(samples[0])):
                ct1_data = samples[0]
                ct2_data = samples[1]
                ct3_data = samples[2]
                ct4_data = samples[3]
                ct5_data = samples[4]
                ct6_data = samples[5]
                v_data = samples[-1]
                writer.writerow([i, ct1_data[i], ct2_data[i], ct3_data[i], ct4_data[i], ct5_data[i], ct6_data[i], v_data[i]])
        logger.info(f"CSV written to {filename}.")

    def get_board_voltage(self):
        """ Take 10 sample readings and return the average board voltage from the +3.3V rail. """
        samples = []
        while len(samples) <= 10:
            data = self.read_adc(self.adc_channels['board_voltage_channel'])
            samples.append(data)

        avg_reading = sum(samples) / len(samples)
        board_voltage = (avg_reading / 1024) * 3.31 * 2
        return board_voltage

    def read_adc(self, adc_num):
        """ Read SPI data from the MCP3008, 8 channels in total. """
        r = self.spi.xfer2([1, 8 + adc_num << 4, 0])
        data = ((r[1] & 3) << 8) + r[2]
        return data

    def collect_data(self, num_samples):
        """  Takes <num_samples> readings from the ADC for each ADC channel and returns a dictionary containing the CT channel number as the key, and a list of that channel's sample data.
        
        Arguments:
        num_samples -- int, the number of samples to collect for each channel.

        Returns a dictionary where the keys are ct1 - ct6, voltage, and time, and the value of each key is a list of that channel's samples (except for 'time', which is a UTC datetime)
        """
        now = datetime.utcnow()  # Get time of reading

        ct1_data = []
        ct2_data = []
        ct3_data = []
        ct4_data = []
        ct5_data = []
        ct6_data = []
        v_data = []

        for _ in range(num_samples):
            ct1 = self.read_adc(self.adc_channels['ct1_channel'])
            ct5 = self.read_adc(self.adc_channels['ct5_channel'])
            ct2 = self.read_adc(self.adc_channels['ct2_channel'])
            v = self.read_adc(self.adc_channels['v_sensor_channel'])
            ct3 = self.read_adc(self.adc_channels['ct3_channel'])
            ct4 = self.read_adc(self.adc_channels['ct4_channel'])
            ct6 = self.read_adc(self.adc_channels['ct6_channel'])
            ct1_data.append(ct1)
            ct2_data.append(ct2)
            ct3_data.append(ct3)
            ct4_data.append(ct4)
            ct5_data.append(ct5)
            ct6_data.append(ct6)
            v_data.append(v)

        samples = {
            'ct1': ct1_data,
            'ct2': ct2_data,
            'ct3': ct3_data,
            'ct4': ct4_data,
            'ct5': ct5_data,
            'ct6': ct6_data,
            'voltage': v_data,
            'time': now,
        }
        return samples

    def calculate_power(self, samples, board_voltage):
        """ Calculates amperage, real power, power factor, and voltage
        
        Arguments:
        samples -- dict, a dictionary containing lists of each channel's sample data, and a tailored voltage wave that's been phase corrected for each corresponding channel. See rebuild_waves() for more info.

        Returns a dictionary containing a dictionary for each channel, with the following structure:
        {
            'ct1': {
                'type': 'consumption',
                'power': <Real Power (float) for this channel>,
                'current': <RMS Current (float) for this channel>,
                'voltage': <RMS Voltage (float)>,
                'pf': <Power Factor (float) for this channel>
            },
            ... ,
            'ct6' : { ... }
        }
        """
        ct1_samples = samples['ct1']        # current samples for ct1
        ct2_samples = samples['ct2']        # current samples for ct2
        ct3_samples = samples['ct3']        # current samples for ct3
        ct4_samples = samples['ct4']        # current samples for ct4
        ct5_samples = samples['ct5']        # current samples for ct5
        ct6_samples = samples['ct6']        # current samples for ct6
        v_samples_1 = samples['v_ct1']      # phase-corrected voltage wave specifically for ct1
        v_samples_2 = samples['v_ct2']      # phase-corrected voltage wave specifically for ct2
        v_samples_3 = samples['v_ct3']      # phase-corrected voltage wave specifically for ct3
        v_samples_4 = samples['v_ct4']      # phase-corrected voltage wave specifically for ct4
        v_samples_5 = samples['v_ct5']      # phase-corrected voltage wave specifically for ct5
        v_samples_6 = samples['v_ct6']      # phase-corrected voltage wave specifically for ct6

        # Variable Initialization
        sum_inst_power_ct1 = 0
        sum_inst_power_ct2 = 0
        sum_inst_power_ct3 = 0
        sum_inst_power_ct4 = 0
        sum_inst_power_ct5 = 0
        sum_inst_power_ct6 = 0
        sum_squared_current_ct1 = 0
        sum_squared_current_ct2 = 0
        sum_squared_current_ct3 = 0
        sum_squared_current_ct4 = 0
        sum_squared_current_ct5 = 0
        sum_squared_current_ct6 = 0
        sum_raw_current_ct1 = 0
        sum_raw_current_ct2 = 0
        sum_raw_current_ct3 = 0
        sum_raw_current_ct4 = 0
        sum_raw_current_ct5 = 0
        sum_raw_current_ct6 = 0
        sum_squared_voltage_1 = 0
        sum_squared_voltage_2 = 0
        sum_squared_voltage_3 = 0
        sum_squared_voltage_4 = 0
        sum_squared_voltage_5 = 0
        sum_squared_voltage_6 = 0
        sum_raw_voltage_1 = 0
        sum_raw_voltage_2 = 0
        sum_raw_voltage_3 = 0
        sum_raw_voltage_4 = 0
        sum_raw_voltage_5 = 0
        sum_raw_voltage_6 = 0

        # Scaling factors
        vref = board_voltage / 1024
        ct1_scaling_factor = vref * 100 * self.accuracy_calibration['ct1']
        ct2_scaling_factor = vref * 100 * self.accuracy_calibration['ct2']
        ct3_scaling_factor = vref * 100 * self.accuracy_calibration['ct3']
        ct4_scaling_factor = vref * 100 * self.accuracy_calibration['ct4']
        ct5_scaling_factor = vref * 100 * self.accuracy_calibration['ct5']
        ct6_scaling_factor = vref * 100 * self.accuracy_calibration['ct6']
        ac_voltage_ratio = (self.grid_voltage / self.ac_transformer_output_voltage) * 11  # Rough approximation
        voltage_scaling_factor = vref * ac_voltage_ratio * self.accuracy_calibration['AC']

        num_samples = len(v_samples_1)

        for i in range(0, num_samples):
            ct1 = (int(ct1_samples[i]))
            ct2 = (int(ct2_samples[i]))
            ct3 = (int(ct3_samples[i]))
            ct4 = (int(ct4_samples[i]))
            ct5 = (int(ct5_samples[i]))
            ct6 = (int(ct6_samples[i]))
            voltage_1 = (int(v_samples_1[i]))
            voltage_2 = (int(v_samples_2[i]))
            voltage_3 = (int(v_samples_3[i]))
            voltage_4 = (int(v_samples_4[i]))
            voltage_5 = (int(v_samples_5[i]))
            voltage_6 = (int(v_samples_6[i]))

            # Process all data in a single function to reduce runtime complexity
            # Get the sum of all current samples individually
            sum_raw_current_ct1 += ct1
            sum_raw_current_ct2 += ct2
            sum_raw_current_ct3 += ct3
            sum_raw_current_ct4 += ct4
            sum_raw_current_ct5 += ct5
            sum_raw_current_ct6 += ct6
            sum_raw_voltage_1 += voltage_1
            sum_raw_voltage_2 += voltage_2
            sum_raw_voltage_3 += voltage_3
            sum_raw_voltage_4 += voltage_4
            sum_raw_voltage_5 += voltage_5
            sum_raw_voltage_6 += voltage_6

            # Calculate instant power for each ct sensor
            inst_power_ct1 = ct1 * voltage_1
            inst_power_ct2 = ct2 * voltage_2
            inst_power_ct3 = ct3 * voltage_3
            inst_power_ct4 = ct4 * voltage_4
            inst_power_ct5 = ct5 * voltage_5
            inst_power_ct6 = ct6 * voltage_6
            sum_inst_power_ct1 += inst_power_ct1
            sum_inst_power_ct2 += inst_power_ct2
            sum_inst_power_ct3 += inst_power_ct3
            sum_inst_power_ct4 += inst_power_ct4
            sum_inst_power_ct5 += inst_power_ct5
            sum_inst_power_ct6 += inst_power_ct6

            # Squared voltage
            squared_voltage_1 = voltage_1 * voltage_1
            squared_voltage_2 = voltage_2 * voltage_2
            squared_voltage_3 = voltage_3 * voltage_3
            squared_voltage_4 = voltage_4 * voltage_4
            squared_voltage_5 = voltage_5 * voltage_5
            squared_voltage_6 = voltage_6 * voltage_6
            sum_squared_voltage_1 += squared_voltage_1
            sum_squared_voltage_2 += squared_voltage_2
            sum_squared_voltage_3 += squared_voltage_3
            sum_squared_voltage_4 += squared_voltage_4
            sum_squared_voltage_5 += squared_voltage_5
            sum_squared_voltage_6 += squared_voltage_6

            # Squared current
            sq_ct1 = ct1 * ct1
            sq_ct2 = ct2 * ct2
            sq_ct3 = ct3 * ct3
            sq_ct4 = ct4 * ct4
            sq_ct5 = ct5 * ct5
            sq_ct6 = ct6 * ct6

            sum_squared_current_ct1 += sq_ct1
            sum_squared_current_ct2 += sq_ct2
            sum_squared_current_ct3 += sq_ct3
            sum_squared_current_ct4 += sq_ct4
            sum_squared_current_ct5 += sq_ct5
            sum_squared_current_ct6 += sq_ct6

        avg_raw_current_ct1 = sum_raw_current_ct1 / num_samples
        avg_raw_current_ct2 = sum_raw_current_ct2 / num_samples
        avg_raw_current_ct3 = sum_raw_current_ct3 / num_samples
        avg_raw_current_ct4 = sum_raw_current_ct4 / num_samples
        avg_raw_current_ct5 = sum_raw_current_ct5 / num_samples
        avg_raw_current_ct6 = sum_raw_current_ct6 / num_samples
        avg_raw_voltage_1 = sum_raw_voltage_1 / num_samples
        avg_raw_voltage_2 = sum_raw_voltage_2 / num_samples
        avg_raw_voltage_3 = sum_raw_voltage_3 / num_samples
        avg_raw_voltage_4 = sum_raw_voltage_4 / num_samples
        avg_raw_voltage_5 = sum_raw_voltage_5 / num_samples
        avg_raw_voltage_6 = sum_raw_voltage_6 / num_samples

        real_power_1 = ((sum_inst_power_ct1 / num_samples) - (avg_raw_current_ct1 * avg_raw_voltage_1))  * ct1_scaling_factor * voltage_scaling_factor
        real_power_2 = ((sum_inst_power_ct2 / num_samples) - (avg_raw_current_ct2 * avg_raw_voltage_2))  * ct2_scaling_factor * voltage_scaling_factor
        real_power_3 = ((sum_inst_power_ct3 / num_samples) - (avg_raw_current_ct3 * avg_raw_voltage_3))  * ct3_scaling_factor * voltage_scaling_factor
        real_power_4 = ((sum_inst_power_ct4 / num_samples) - (avg_raw_current_ct4 * avg_raw_voltage_4))  * ct4_scaling_factor * voltage_scaling_factor
        real_power_5 = ((sum_inst_power_ct5 / num_samples) - (avg_raw_current_ct5 * avg_raw_voltage_5))  * ct5_scaling_factor * voltage_scaling_factor
        real_power_6 = ((sum_inst_power_ct6 / num_samples) - (avg_raw_current_ct6 * avg_raw_voltage_6))  * ct6_scaling_factor * voltage_scaling_factor

        mean_square_current_ct1 = sum_squared_current_ct1 / num_samples
        mean_square_current_ct2 = sum_squared_current_ct2 / num_samples
        mean_square_current_ct3 = sum_squared_current_ct3 / num_samples
        mean_square_current_ct4 = sum_squared_current_ct4 / num_samples
        mean_square_current_ct5 = sum_squared_current_ct5 / num_samples
        mean_square_current_ct6 = sum_squared_current_ct6 / num_samples
        mean_square_voltage_1 = sum_squared_voltage_1 / num_samples
        mean_square_voltage_2 = sum_squared_voltage_2 / num_samples
        mean_square_voltage_3 = sum_squared_voltage_3 / num_samples
        mean_square_voltage_4 = sum_squared_voltage_4 / num_samples
        mean_square_voltage_5 = sum_squared_voltage_5 / num_samples
        mean_square_voltage_6 = sum_squared_voltage_6 / num_samples

        rms_current_ct1 = sqrt(mean_square_current_ct1 - (avg_raw_current_ct1 * avg_raw_current_ct1)) * ct1_scaling_factor
        rms_current_ct2 = sqrt(mean_square_current_ct2 - (avg_raw_current_ct2 * avg_raw_current_ct2)) * ct2_scaling_factor
        rms_current_ct3 = sqrt(mean_square_current_ct3 - (avg_raw_current_ct3 * avg_raw_current_ct3)) * ct3_scaling_factor
        rms_current_ct4 = sqrt(mean_square_current_ct4 - (avg_raw_current_ct4 * avg_raw_current_ct4)) * ct4_scaling_factor
        rms_current_ct5 = sqrt(mean_square_current_ct5 - (avg_raw_current_ct5 * avg_raw_current_ct5)) * ct5_scaling_factor
        rms_current_ct6 = sqrt(mean_square_current_ct6 - (avg_raw_current_ct6 * avg_raw_current_ct6)) * ct6_scaling_factor
        rms_voltage_1 = sqrt(mean_square_voltage_1 - (avg_raw_voltage_1 * avg_raw_voltage_1)) * voltage_scaling_factor
        rms_voltage_2 = sqrt(mean_square_voltage_2 - (avg_raw_voltage_2 * avg_raw_voltage_2)) * voltage_scaling_factor
        rms_voltage_3 = sqrt(mean_square_voltage_3 - (avg_raw_voltage_3 * avg_raw_voltage_3)) * voltage_scaling_factor
        rms_voltage_4 = sqrt(mean_square_voltage_4 - (avg_raw_voltage_4 * avg_raw_voltage_4)) * voltage_scaling_factor
        rms_voltage_5 = sqrt(mean_square_voltage_5 - (avg_raw_voltage_5 * avg_raw_voltage_5)) * voltage_scaling_factor
        rms_voltage_6 = sqrt(mean_square_voltage_6 - (avg_raw_voltage_6 * avg_raw_voltage_6)) * voltage_scaling_factor

        # Power Factor
        apparent_power_1 = rms_voltage_1 * rms_current_ct1
        apparent_power_2 = rms_voltage_2 * rms_current_ct2
        apparent_power_3 = rms_voltage_3 * rms_current_ct3
        apparent_power_4 = rms_voltage_4 * rms_current_ct4
        apparent_power_5 = rms_voltage_5 * rms_current_ct5
        apparent_power_6 = rms_voltage_6 * rms_current_ct6

        try:
            power_factor_1 = real_power_1 / apparent_power_1
        except ZeroDivisionError:
            power_factor_1 = 0
        try:
            power_factor_2 = real_power_2 / apparent_power_2
        except ZeroDivisionError:
            power_factor_2 = 0
        try:
            power_factor_3 = real_power_3 / apparent_power_3
        except ZeroDivisionError:
            power_factor_3 = 0
        try:
            power_factor_4 = real_power_4 / apparent_power_4
        except ZeroDivisionError:
            power_factor_4 = 0
        try:
            power_factor_5 = real_power_5 / apparent_power_5
        except ZeroDivisionError:
            power_factor_5 = 0
        try:
            power_factor_6 = real_power_6 / apparent_power_6
        except ZeroDivisionError:
            power_factor_6 = 0

        results = {
            'ct1': {
                'type': 'consumption',
                'power': real_power_1,
                'current': rms_current_ct1,
                'voltage': rms_voltage_1,
                'pf': power_factor_1
            },
            'ct2': {
                'type': 'consumption',
                'power': real_power_2,
                'current': rms_current_ct2,
                'voltage': rms_voltage_2,
                'pf': power_factor_2
            },
            'ct3': {
                'type': 'consumption',
                'power': real_power_3,
                'current': rms_current_ct3,
                'voltage': rms_voltage_3,
                'pf': power_factor_3
            },
            'ct4': {
                'type': 'consumption',
                'power': real_power_4,
                'current': rms_current_ct4,
                'voltage': rms_voltage_4,
                'pf': power_factor_4
            },
            'ct5': {
                'type': 'consumption',
                'power': real_power_5,
                'current': rms_current_ct5,
                'voltage': rms_voltage_5,
                'pf': power_factor_5
            },
            'ct6': {
                'type': 'consumption',
                'power': real_power_6,
                'current': rms_current_ct6,
                'voltage': rms_voltage_6,
                'pf': power_factor_6
            },
            'voltage': rms_voltage_1,
        }

        return results

    @staticmethod
    def rebuild_waves(samples, PHASECAL_1, PHASECAL_2, PHASECAL_3, PHASECAL_4, PHASECAL_5, PHASECAL_6):
        """ Adjusts the sampled voltage wave to correct for the phase error introduced by time differences between the voltage sample and each channel's current sample.
        
        Arguments:
        samples     -- dict, a dictionary containing lists of each channel's sample data, and a tailored voltage wave that's been phase corrected for each corresponding channel.
        PHASE_CAL_1 -- float, the phase correction constant for channel 1
        PHASE_CAL_2 -- float, the phase correction constant for channel 2
        PHASE_CAL_3 -- float, the phase correction constant for channel 3
        PHASE_CAL_4 -- float, the phase correction constant for channel 4
        PHASE_CAL_5 -- float, the phase correction constant for channel 5
        PHASE_CAL_6 -- float, the phase correction constant for channel 6

        Returns a dictionary where the keys are ct1 through ct6, v_ct1 through v_ct6, and voltage. The ct1 - ct6 keys contains a list of the original current samples taken for each channel. 
        The v_ct1 - v_ct6 keys contain a list of the phase-corrected voltage samples corresponding to each channel.
        """
        wave_1 = []
        wave_2 = []
        wave_3 = []
        wave_4 = []
        wave_5 = []
        wave_6 = []

        voltage_samples = samples['voltage']

        wave_1.append(voltage_samples[0])
        wave_2.append(voltage_samples[0])
        wave_3.append(voltage_samples[0])
        wave_4.append(voltage_samples[0])
        wave_5.append(voltage_samples[0])
        wave_6.append(voltage_samples[0])
        previous_point = voltage_samples[0]

        for current_point in voltage_samples[1:]:
            new_point_1 = previous_point + PHASECAL_1 * (current_point - previous_point)
            new_point_2 = previous_point + PHASECAL_2 * (current_point - previous_point)
            new_point_3 = previous_point + PHASECAL_3 * (current_point - previous_point)
            new_point_4 = previous_point + PHASECAL_4 * (current_point - previous_point)
            new_point_5 = previous_point + PHASECAL_5 * (current_point - previous_point)
            new_point_6 = previous_point + PHASECAL_6 * (current_point - previous_point)

            wave_1.append(new_point_1)
            wave_2.append(new_point_2)
            wave_3.append(new_point_3)
            wave_4.append(new_point_4)
            wave_5.append(new_point_5)
            wave_6.append(new_point_6)

            previous_point = current_point

        rebuilt_waves = {
            'v_ct1': wave_1,
            'v_ct2': wave_2,
            'v_ct3': wave_3,
            'v_ct4': wave_4,
            'v_ct5': wave_5,
            'v_ct6': wave_6,
            'voltage': voltage_samples,
            'ct1': samples['ct1'],
            'ct2': samples['ct2'],
            'ct3': samples['ct3'],
            'ct4': samples['ct4'],
            'ct5': samples['ct5'],
            'ct6': samples['ct6'],
        }

        return rebuilt_waves

    def run_main(self):
        """ Starts the main power monitor loop. """
        logger.info("... Starting Raspberry Pi Power Monitor")
        logger.info("Press Ctrl-c to quit...")
        # The following empty dictionaries will hold the respective calculated values at the end
        # of each polling cycle, which are then averaged prior to storing the value to the DB.
        solar_power_values = dict(power=[], pf=[], current=[])
        home_load_values = dict(power=[], pf=[], current=[])
        net_power_values = dict(power=[], current=[])
        ct1_dict = dict(power=[], pf=[], current=[])
        ct2_dict = dict(power=[], pf=[], current=[])
        ct3_dict = dict(power=[], pf=[], current=[])
        ct4_dict = dict(power=[], pf=[], current=[])
        ct5_dict = dict(power=[], pf=[], current=[])
        ct6_dict = dict(power=[], pf=[], current=[])
        rms_voltages = []
        i = 0   # Counter for aggregate function

        while True:
            try:
                board_voltage = self.get_board_voltage()
                samples = self.collect_data(2000)
                poll_time = samples['time']

                # ct1_samples = samples['ct1']
                # ct2_samples = samples['ct2']
                # ct3_samples = samples['ct3']
                # ct4_samples = samples['ct4']
                # ct5_samples = samples['ct5']
                # ct6_samples = samples['ct6']
                # v_samples = samples['voltage']

                rebuilt_waves = self.rebuild_waves(
                    samples,
                    self.ct_phase_correction['ct1'],
                    self.ct_phase_correction['ct2'],
                    self.ct_phase_correction['ct3'],
                    self.ct_phase_correction['ct4'],
                    self.ct_phase_correction['ct5'],
                    self.ct_phase_correction['ct6'])
                results = self.calculate_power(rebuilt_waves, board_voltage)

                # RMS calculation for phase correction only - this is not needed after everything is tuned.
                # The following code is used to compare the RMS power to the calculated real power.
                # Ideally, you want the RMS power to equal the real power when measuring a purely resistive load.
                # rms_power_1 = round(results['ct1']['current'] * results['ct1']['voltage'], 2)  # AKA apparent power
                # rms_power_2 = round(results['ct2']['current'] * results['ct2']['voltage'], 2)  # AKA apparent power
                # rms_power_3 = round(results['ct3']['current'] * results['ct3']['voltage'], 2)  # AKA apparent power
                # rms_power_4 = round(results['ct4']['current'] * results['ct4']['voltage'], 2)  # AKA apparent power
                # rms_power_5 = round(results['ct5']['current'] * results['ct5']['voltage'], 2)  # AKA apparent power
                # rms_power_6 = round(results['ct6']['current'] * results['ct6']['voltage'], 2)  # AKA apparent power

                # Prepare values for database storage
                grid_1_power = results['ct1']['power']    # ct1 Real Power
                grid_2_power = results['ct2']['power']    # ct2 Real Power
                grid_3_power = results['ct3']['power']    # ct3 Real Power
                grid_4_power = results['ct4']['power']    # ct4 Real Power
                grid_5_power = results['ct5']['power']    # ct5 Real Power
                grid_6_power = results['ct6']['power']    # ct6 Real Power

                grid_1_current = results['ct1']['current']  # ct1 Current
                grid_2_current = results['ct2']['current']  # ct2 Current
                grid_3_current = results['ct3']['current']  # ct3 Current
                grid_4_current = results['ct4']['current']  # ct4 Current
                grid_5_current = results['ct5']['current']  # ct5 Current
                grid_6_current = results['ct6']['current']  # ct6 Current

                # If you are monitoring solar/generator inputs to your panel,
                # specify which CT number(s) you are using, and uncomment the commented lines.
                solar_power = 0
                solar_current = 0
                solar_pf = 0
                # solar_power = results['ct4']['power']
                # solar_current = results['ct4']['current']
                # solar_pf = results['ct4']['pf']
                voltage = results['voltage']

                # Set solar power and current to zero if the solar power is under 20W.
                if solar_power < 20:
                    solar_power = 0
                    solar_current = 0
                    solar_pf = 0

                # Determine if the system is net producing or net consuming right now by looking at the two panel mains.
                # Since the current measured is always positive,
                # we need to add a negative sign to the amperage value if we're exporting power.
                if grid_1_power < 0:
                    grid_1_current = grid_1_current * -1
                if grid_2_power < 0:
                    grid_2_current = grid_2_current * -1
                if solar_power > 0:
                    solar_current = solar_current * -1

                # Unless your specific panel setup matches mine exactly,
                # the following four lines will likely need to be re-written:
                home_consumption_power = (
                        grid_1_power + grid_2_power + grid_3_power +
                        grid_4_power + grid_5_power + grid_6_power + solar_power)
                net_power = home_consumption_power - solar_power
                home_consumption_current = (
                        grid_1_current + grid_2_current + grid_3_current +
                        grid_4_current + grid_5_current + grid_6_current - solar_current)
                net_current = (
                        grid_1_current + grid_2_current + grid_3_current +
                        grid_4_current + grid_5_current + grid_6_current + solar_current)

                # if net_power < 0:
                #     current_status = "Producing"
                # else:
                #     current_status = "Consuming"

                # Average 2 readings before sending to db
                if i < 2:
                    solar_power_values['power'].append(solar_power)
                    solar_power_values['current'].append(solar_current)
                    solar_power_values['pf'].append(solar_pf)

                    home_load_values['power'].append(home_consumption_power)
                    home_load_values['current'].append(home_consumption_current)
                    net_power_values['power'].append(net_power)
                    net_power_values['current'].append(net_current)

                    ct1_dict['power'].append(results['ct1']['power'])
                    ct1_dict['current'].append(results['ct1']['current'])
                    ct1_dict['pf'].append(results['ct1']['pf'])
                    ct2_dict['power'].append(results['ct2']['power'])
                    ct2_dict['current'].append(results['ct2']['current'])
                    ct2_dict['pf'].append(results['ct2']['pf'])
                    ct3_dict['power'].append(results['ct3']['power'])
                    ct3_dict['current'].append(results['ct3']['current'])
                    ct3_dict['pf'].append(results['ct3']['pf'])
                    ct4_dict['power'].append(results['ct4']['power'])
                    ct4_dict['current'].append(results['ct4']['current'])
                    ct4_dict['pf'].append(results['ct4']['pf'])
                    ct5_dict['power'].append(results['ct5']['power'])
                    ct5_dict['current'].append(results['ct5']['current'])
                    ct5_dict['pf'].append(results['ct5']['pf'])
                    ct6_dict['power'].append(results['ct6']['power'])
                    ct6_dict['current'].append(results['ct6']['current'])
                    ct6_dict['pf'].append(results['ct6']['pf'])
                    rms_voltages.append(voltage)
                    i += 1
                else:
                    # Calculate the average, send the result to InfluxDB
                    # and reset the dictionaries for the next 2 sets of data.
                    infl.write_to_influx(
                        solar_power_values,
                        home_load_values,
                        net_power_values,
                        ct1_dict,
                        ct2_dict,
                        ct3_dict,
                        ct4_dict,
                        ct5_dict,
                        ct6_dict,
                        poll_time,
                        i,
                        rms_voltages)
                    solar_power_values = dict(power=[], pf=[], current=[])
                    home_load_values = dict(power=[], pf=[], current=[])
                    net_power_values = dict(power=[], current=[])
                    ct1_dict = dict(power=[], pf=[], current=[])
                    ct2_dict = dict(power=[], pf=[], current=[])
                    ct3_dict = dict(power=[], pf=[], current=[])
                    ct4_dict = dict(power=[], pf=[], current=[])
                    ct5_dict = dict(power=[], pf=[], current=[])
                    ct6_dict = dict(power=[], pf=[], current=[])
                    rms_voltages = []
                    i = 0

                    if logger.handlers[0].level == 10:
                        self.print_results(results)

                # sleep(0.1)

            except KeyboardInterrupt:
                infl.close_db()
                sys.exit()

    @staticmethod
    def rebuild_wave(samples, v_wave, PHASECAL):
        """ Rebuilds a single voltage wave by applying the PHASECAL constant to the voltage samples contained in v_wave.

        Arguments:
        samples     -- list, contains the raw ADC readings from a single CT input
        v_wave      -- list, contians raw ADC readings from the original voltage waveform
        PHASECAL:   -- float, the phase correction constant for this channel.
        """
        

        # The following empty lists will hold the phase corrected voltage wave that corresponds to each individual CT sensor.
        wave = []

        wave.append(v_wave[0])
        previous_point = v_wave[0]

        for current_point in v_wave[1:]:
            new_point = previous_point + PHASECAL * (current_point - previous_point)
            wave.append(new_point)
            previous_point = current_point

        rebuilt_wave = {
            'new_v': wave,  # Rebuilt voltage wave
            'ct': samples,  # Raw ADC output for a single CT
            'original_v': v_wave,  # Original voltage wave samples
        }
        return rebuilt_wave

    @staticmethod
    def check_phasecal(samples, rebuilt_wave, board_voltage):
        """ This function is a trimmed down version of the calculate_power(). It's primary purpose is to aid in the finding of the ideal
        PHASECAL constant for this channel by calculating the power using the already-phase-corrected rebuilt_wave.
        
        Arguments:
        samples         -- list, raw ADC output values for a single CT
        rebuilt_wave    -- list, phase-corrected voltage wave for the single CT
        board_voltage   -- float, current reading of the reference voltage from the +3.3V rail

        Returns a dictionary containing the power, current, voltage, and power factor (pf) for this channel so that the caller can determine if the
        PHASECAL constant applied for this check was better than the previous PHASECAL constant.
        """

        # Variable Initialization
        sum_inst_power = 0
        sum_squared_current = 0
        sum_raw_current = 0
        sum_squared_voltage = 0
        sum_raw_voltage = 0

        # Scaling factors
        vref = board_voltage / 1024
        # ct_scaling_factor = vref * 100 * ct_accuracy_factor
        ct_scaling_factor = vref * 100
        # voltage_scaling_factor = vref * 126.5 * AC_voltage_accuracy_factor
        voltage_scaling_factor = vref * 126.5

        num_samples = len(rebuilt_wave)

        for i in range(0, num_samples):
            ct = (int(samples[i]))
            voltage = (int(rebuilt_wave[i]))

            # Process all data in a single function to reduce runtime complexity
            # Get the sum of all current samples individually
            sum_raw_current += ct
            sum_raw_voltage += voltage

            # Calculate instant power for each ct sensor
            inst_power = ct * voltage
            sum_inst_power += inst_power

            # Squared voltage
            squared_voltage = voltage * voltage
            sum_squared_voltage += squared_voltage

            # Squared current
            sq_ct = ct * ct
            sum_squared_current += sq_ct

        avg_raw_current = sum_raw_current / num_samples
        avg_raw_voltage = sum_raw_voltage / num_samples

        real_power = ((sum_inst_power / num_samples) - (
                    avg_raw_current * avg_raw_voltage)) * ct_scaling_factor * voltage_scaling_factor

        mean_square_current = sum_squared_current / num_samples
        mean_square_voltage = sum_squared_voltage / num_samples

        rms_current = sqrt(mean_square_current - (avg_raw_current * avg_raw_current)) * ct_scaling_factor
        rms_voltage = sqrt(mean_square_voltage - (avg_raw_voltage * avg_raw_voltage)) * voltage_scaling_factor

        apparent_power = rms_voltage * rms_current

        try:
            power_factor = real_power / apparent_power
        except ZeroDivisionError:
            power_factor = 0

        results = {
            'power': real_power,
            'current': rms_current,
            'voltage': rms_voltage,
            'pf': power_factor
        }

        return results

    def find_phasecal(self, samples, ct_selection, accuracy_digits, board_voltage):
        """ Determines the indeal PHASECAL constant to achieve a power factor closest to 1.  Assumes that the user is measuring a purely resistive load.
        
        Arguments:
        samples         -- dict, a dictionary containing lists of each channel's sample data
        ct_selection    -- int, the channel number selected by the user
        accuracy_digits -- int, currently unused, but would control the rounding of the measured PF. It's currently hardcoded to 4 below.
        board_voltage   -- float, the latest measured board voltage from the +3.3 rail.

        Returns a list of dicts, where each dict holds the best power factor values and the corresponding PHASECAL constant that was used to calculate the PF.
        """
        # This controls how many times the calibration process is repeated for this particular CT.
        num_calibration_attempts = 20

        # Get Initial PF
        rebuilt_wave = self.rebuild_wave(samples[ct_selection], samples['voltage'], 1.0)
        results = self.check_phasecal(rebuilt_wave['ct'], rebuilt_wave['new_v'], board_voltage)
        pf = results['pf']
        logger.debug(
            f"Please wait while I read {ct_selection} and calculate the best PHASECAL value. This can take a few minutes, so please be patient.")

        best_pfs = []
        previous_phasecal = 1.0
        previous_pf = pf
        trends = []

        # Base Increment settings for changing phasecal
        increment = 1.005
        decrement = 0.995
        big_increment = 1.01
        big_decrement = 0.98

        for i, _ in enumerate(range(3), start=1):
            best_pf = {
                'pf': 0,
                'cal': 0
            }
            for _ in range(75):

                if round(pf, 4) == 1.0:
                    best_pf.update({
                        'pf': pf,
                        'cal': new_phasecal,
                    })
                    break

                if pf < 1.0:
                    # If the PF isn't better than 0.995, we can increment the phasecal by an amount twice as large, referred to as big_increment, to help speed up the process.
                    if round(pf, 2) != 1.0:
                        new_phasecal = previous_phasecal * big_increment
                    else:
                        new_phasecal = previous_phasecal * increment
                    action = 'incremented'
                else:
                    if round(pf, 2) != 1.0:
                        new_phasecal = previous_phasecal * big_decrement
                    else:
                        new_phasecal = previous_phasecal * decrement
                    action = 'decremented'

                # Collect a live sample and calculate PF using new_phasecal
                samples = self.collect_data(2000)
                rebuilt_wave = self.rebuild_wave(samples[ct_selection], samples['voltage'], new_phasecal)
                results = self.check_phasecal(rebuilt_wave['ct'], rebuilt_wave['new_v'], board_voltage)
                pf = results['pf']
                if pf > best_pf['pf']:
                    best_pf.update({
                        'pf': pf,
                        'cal': new_phasecal
                    })

                # logger.debug(f"  PF: {pf} | Phasecal: {new_phasecal}")

                # Determine whether or not trend is moving away from 1.0 or towards 1.0.
                # Trend should be a moving average over two values.

                trends.append(pf - previous_pf)
                previous_phasecal = new_phasecal

                if len(trends) == 2:
                    # Check to see if both values have the same sign to determine the actual trend, then empty the list
                    if trends[0] < 0:
                        if trends[1] < 0:
                            trend = 'worse'
                            # If the trend is getting worse, reject the previous phasecal, and reduce cut increment/decrement by half.
                            increment = 1 + (abs(1 - increment) / 2)
                            decrement = decrement + ((1 - decrement) / 2)

                            # Apply the opposite action to the previous phasecal value to attempt to reverse the trend. If the previous phasecal
                            # was incremented, then we will decrement using the newly adjusted decrement value.
                            if action == 'increment':
                                # Decrement instead
                                new_phasecal = previous_phasecal * decrement
                            else:
                                # Increment instead
                                new_phasecal = previous_phasecal * increment

                    if trends[1] > 0:
                        trend = 'better'

                    trends = []
                    continue  # Skip updating the previous phasecal and previous_pf since we want to attempt to reverse the trend.

                else:
                    if action == 'increment':
                        # Repeat same action
                        new_phasecal = previous_phasecal * increment
                    else:
                        new_phasecal = previous_phasecal * decrement

                previous_pf = pf

            logger.debug(f"Wave {i}/3 results: ")
            logger.debug(f" Best PF: {best_pf['pf']} using phasecal: {best_pf['cal']}")
            best_pfs.append(best_pf)

        return best_pfs

    @staticmethod
    def print_results(results):
        t = PrettyTable(['', 'ct1', 'ct2', 'ct3', 'ct4', 'ct5', 'ct6'])
        t.add_row(['Watts',
                   round(results['ct1']['power'], 3),
                   round(results['ct2']['power'], 3),
                   round(results['ct3']['power'], 3),
                   round(results['ct4']['power'], 3),
                   round(results['ct5']['power'], 3),
                   round(results['ct6']['power'], 3)])
        t.add_row(['Current',
                   round(results['ct1']['current'], 3),
                   round(results['ct2']['current'], 3),
                   round(results['ct3']['current'], 3),
                   round(results['ct4']['current'], 3),
                   round(results['ct5']['current'], 3),
                   round(results['ct6']['current'], 3)])
        t.add_row(['P.F.',
                   round(results['ct1']['pf'], 3),
                   round(results['ct2']['pf'], 3),
                   round(results['ct3']['pf'], 3),
                   round(results['ct4']['pf'], 3),
                   round(results['ct5']['pf'], 3),
                   round(results['ct6']['pf'], 3)])
        t.add_row(['Voltage', round(results['voltage'], 3), '', '', '', '', ''])
        s = t.get_string()
        logger.debug(f"\n{s}")

    @staticmethod
    def get_ip():
        """ Determines your Pi's local IP address so that it can be displayed to the user for ease of accessing generated plots. 
        
        Returns a string representing the Pi's local IP address that's associated with the default route.
        """
        
        s = socket(AF_INET, SOCK_DGRAM)
        try:
            s.connect(('10.255.255.255', 1))
            ip = s.getsockname()[0]
        except:
            ip = None
        finally:
            s.close()
        return ip


if __name__ == '__main__':
    try:  # Backup config.py file
        copyfile('config.py', 'config.py.backup')
    except FileNotFoundError:
        logger.info("Could not create a backup of config.py file.")

    rpm = RPiPowerMonitor()

    if len(sys.argv) > 1:
        MODE = sys.argv[1]
        if MODE == 'debug' or MODE == 'phase':
            try:
                title = sys.argv[2]
            except IndexError:
                title = None
        try:
            os.makedirs('data/samples/')  # Create the data/samples directory:
        except FileExistsError:
            pass
    else:
        MODE = None

    if not MODE:
        # Try to establish a connection to the DB for 5 seconds:
        x = 0
        connection_established = False
        logger.info(f"... Trying to connect to database at: {db_settings['host']}:{db_settings['port']}")
        while x < 5:
            connection_established = infl.init_db()
            if connection_established:
                break
            else:
                sleep(1)
                x += 1

        if not connection_established:
            if (db_settings['host'] == 'localhost' or
                    '127.0' in db_settings['host'] or
                    rpm.get_ip() in db_settings['host']):
                logger.critical(f"Could not connect to InfluxDB on this Pi. Please check the status of Influx with 'sudo systemctl status influxdb'.")
                sys.exit()
            else:
                logger.info(
                    f"Could not connect to your remote database at {db_settings['host']}:{db_settings['port']}. "
                    f"Please verify connectivity/credentials and try again.")
                sys.exit()
        else:
            rpm.run_main()

    else:
        # Program launched in one of the non-main modes. Increase logging level.
        logger.setLevel(logging.DEBUG)
        logger.handlers[0].setLevel(logging.DEBUG)      
        if 'help' in MODE.lower() or '-h' in MODE.lower():

            logger.info("See the project Wiki for more detailed usage instructions: "
                        "https://github.com/David00/rpi-power-monitor/wiki")
            logger.info(dedent("""Usage:
                Start the program:                                  python3 power-monitor.py

                Collect raw data and build an interactive plot:     python3 power-monitor.py debug "chart title here" 

                Launch interactive phase correction mode:           python3 power-monitor.py phase

                Start the program like normal, but print all        python3 power-monitor.py terminal
                readings to the terminal window
                """))

        if MODE.lower() == 'debug':
            # This mode is intended to take a look at the raw CT sensor data.
            # It will take 2000 samples from each CT sensor, plot them to a single chart,
            # write the chart to an HTML file located in /var/www/html/, and then terminate.
            # It also stores the samples to a file located in ./data/samples/last-debug.pkl
            # so that the sample data can be read when this program is started in 'phase' mode.

            # Time sample collection
            start = timeit.default_timer()
            samples = rpm.collect_data(2000)
            stop = timeit.default_timer()
            duration = stop - start

            # Calculate Sample Rate in Kilo-Samples Per Second.
            sample_count = sum([len(samples[x]) for x in samples.keys() if type(samples[x]) == list])
            
            sample_rate = round((sample_count / duration) / 1000, 2)

            logger.debug(f"Finished Collecting Samples. Sample Rate: {sample_rate} KSPS")
            ct1_samples = samples['ct1']
            ct2_samples = samples['ct2']
            ct3_samples = samples['ct3']
            ct4_samples = samples['ct4']
            ct5_samples = samples['ct5']
            ct6_samples = samples['ct6']
            v_samples = samples['voltage']

            # Save samples to disk
            with open('data/samples/last-debug.pkl', 'wb') as f:
                pickle.dump(samples, f)

            if not title:
                title = input("Enter the title for this chart: ")

            title = title.replace(" ", "_")
            logger.debug("Building plot.")
            plot_data(samples, title, sample_rate=sample_rate)
            ip = rpm.get_ip()
            if ip:
                logger.info(
                    f"Chart created! Visit http://{ip}/{title}.html to view the chart. Or, "
                    f"simply visit http://{ip} to view all the charts created using 'debug' and/or 'phase' mode.")
            else:
                logger.info(
                    "Chart created! I could not determine the IP address of this machine. "
                    "Visit your device's IP address in a webrowser to view the list of charts "
                    "you've created using 'debug' and/or 'phase' mode.")

        if MODE.lower() == 'phase':
            # This mode is intended to be used for correcting the phase error in your CT sensors.
            # Please ensure that you have a purely resistive load running through your CT sensors.
            # That means no electric fans and no digital circuitry!
            PF_ROUNDING_DIGITS = 3  # This variable controls how many decimal places the PF will be rounded

            while True:
                try:    
                    ct_num = int(input("\nWhich CT number are you calibrating? Enter the number of the CT label [1 - 6]: "))
                    if ct_num not in range(1, 7):
                        logger.error("Please choose from CT numbers 1, 2, 3, 4, 5, or 6.")
                    else:
                        ct_selection = f'ct{ct_num}'
                        break
                except ValueError:
                    logger.error("Please enter an integer! Acceptable choices are: 1, 2, 3, 4, 5, 6.")

            cont = input(dedent(f"""\n
                #--------------------------------------------------------------------------------#
                # IMPORTANT: Make sure that current transformer {ct_selection} is installed over #
                #            a purely resistive load and that the load is turned on              #
                #            before continuing with the calibration!                             #
                #--------------------------------------------------------------------------------#

                Continue? [y/yes/n/no]: """))

            if cont.lower() in ['n', 'no']:
                logger.info("\nCalibration Aborted.\n")
                sys.exit()

            samples = rpm.collect_data(2000)
            rebuilt_wave = rpm.rebuild_wave(
                samples[ct_selection], samples['voltage'], rpm.ct_phase_correction[ct_selection])
            board_voltage = rpm.get_board_voltage()
            results = rpm.check_phasecal(rebuilt_wave['ct'], rebuilt_wave['new_v'], board_voltage)

            # Get the current power factor and check to make sure it is not negative.
            # If it is, the CT is installed opposite to how it should be.
            pf = results['pf']
            initial_pf = pf  
            if pf < 0:
                logger.info(dedent('''
                    Current transformer is installed backwards. Please reverse the direction that it is attached to your load. \n
                    (Unclip it from your conductor, and clip it on so that the current flows the opposite direction from the CT's perspective) \n
                    Press ENTER to continue when you've reversed your CT.'''))
                input("[ENTER]")
                # Check to make sure the CT was reversed properly by taking another batch of samples/calculations:
                samples = rpm.collect_data(2000)
                rebuilt_wave = rpm.rebuild_wave(samples[ct_selection], samples['voltage'], 1)
                board_voltage = rpm.get_board_voltage()
                results = rpm.check_phasecal(rebuilt_wave['ct'], rebuilt_wave['new_v'], board_voltage)
                pf = results['pf']
                if pf < 0:
                    logger.info(dedent("""It still looks like the current transformer is installed backwards. Are you sure this is a resistive load?\n
                        Please consult the project documentation on https://github.com/david00/rpi-power-monitor/wiki and try again."""))
                    sys.exit()

            # Initialize phasecal values
            new_phasecal = rpm.ct_phase_correction[ct_selection]
            previous_pf = 0
            new_pf = pf

            samples = rpm.collect_data(2000)
            board_voltage = rpm.get_board_voltage()
            best_pfs = rpm.find_phasecal(samples, ct_selection, PF_ROUNDING_DIGITS, board_voltage)
            avg_phasecal = sum([x['cal'] for x in best_pfs]) / len([x['cal'] for x in best_pfs])
            logger.info(f"Please update the value for {ct_selection} in ct_phase_correction "
                        f"in config.py with the following value: {round(avg_phasecal, 8)}")
            logger.info("Please wait... building HTML plot...")
            # Get new set of samples using recommended phasecal value
            samples = rpm.collect_data(2000)
            rebuilt_wave = rpm.rebuild_wave(samples[ct_selection], samples['voltage'], avg_phasecal)

            report_title = f'CT{ct_num}-phase-correction-result'
            plot_data(rebuilt_wave, report_title, ct_selection)
            logger.info(f"file written to {report_title}.html")

        if MODE.lower() == "terminal":
            # This mode will read the sensors, perform the calculations, and print the wattage,
            # current, power factor, and voltage to the terminal.
            # Data is stored to the database in this mode!
            logger.debug("... Starting program in terminal mode")
            
            connection_established = infl.init_db()
            
            if not connection_established:
                # Check to see if the user's DB configuration points to this Pi:
                if (db_settings['host'] == 'localhost' or
                        '127.0' in db_settings['host'] or
                        rpm.get_ip() in db_settings['host']):
                    logger.critical("Unable to connect to InfluxDB on this Pi. Please check the database status with 'sudo systemctl status influxdb' to diagnose the issue.")
                    sys.exit()
                else:
                    logger.info(
                        "Could not connect to your remote database. "
                        "Please verify this Pi can connect to your database and then try running the software again.")
                    sys.exit()

            rpm.run_main()
