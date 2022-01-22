# Power Monitor HAT (for Raspberry Pi)

This project is forked from https://github.com/David00/rpi-power-monitor.
It has been tweaked by me to add day/night tariff sensing (via GPIO17 and a dc PSU on the night rate mains supply) and to more easily display the power and costs of the breakers not monitored by discrete CTs (ie) the whole load is sensed by CT0; The other CTs are subtracted from this load and presented as 'ct_delta_load' (formerly 'home_load').

See https://github.com/David00/rpi-power-monitor for all other documentation etc.

This project is derived from and inspired by the resources located at https://learn.openenergymonitor.org. 


###### Last Updated:  January 2022
