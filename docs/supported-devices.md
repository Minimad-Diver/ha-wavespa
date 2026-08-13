# Supported devices

Wavespa devices change over time, so new devices can sometimes require changes to the integration to add support. This page attempts to keep track of how each device communicates, and the support status for each of these protocols.

## Known products

Raise an issue or log a pull request to update this list. Please do not suggest updates based on marketing material - only trust values that have been observed in the integration logs.

Support is determined by the `product_name` the device reports, not by its
marketing name. Two are recognised:

| Reported `product_name` | Region | Temperature unit | Protocol |
| ----------------------- | ------ | ---------------- | -------- |
| `Wave_SPA_EU`           | EU     | Celsius          | standard |
| `Wave_SPA_US`           | US     | Fahrenheit       | standard |

Any other `product_name` is treated as an unknown device: the diagnostic
sensors are still created, but the thermostat, switches and estimated power
sensors are not, and the integration logs the attributes it received at
warning level so support can be added.

## Known models

| Model          | Reported `product_name` | Protocol |
| -------------- | ----------------------- | -------- |
| Wave Spa Garda | `Wave_SPA_EU`           | standard |

## Protocol support

| Protocol | Supported          |
| -------- | ------------------ |
| standard | :white_check_mark: |
