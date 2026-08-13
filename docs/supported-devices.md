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

## Observed attributes

What a device actually reports, from integration debug logs. Useful when adding
support for a new model, or working out which attribute drives a feature.

`Wave_SPA_EU`, observed 2026-08-13 with the spa above its target temperature:

| Attribute               | Example | Used by the integration         |
| ----------------------- | ------- | ------------------------------- |
| `Current_temperature`   | `26`    | Thermostat, heating state       |
| `Temperature_setup`     | `24`    | Thermostat, heating state       |
| `Heater`                | `1`     | Thermostat mode, power estimate |
| `Filter`                | `1`     | Filter switch, power estimate   |
| `Bubble`                | `0`     | Bubbles switch, power estimate  |
| `Time_filter`           | `22`    | Filter life sensor              |
| `Fp`                    | `0`     | Not used                        |
| `Overtime_filter`       | `0`     | Not used                        |
| `Superheat`             | `0`     | Not used                        |
| `Undercooling`          | `0`     | Not used                        |
| `Temp1` `Temp2` `Temp3` | `0`     | Not used                        |
| `bit1` `bit2`           | `0`     | Not used                        |

Two things worth knowing about this sample:

- `Heater` is `1` while the water is 26 °C against a 24 °C target. The flag
  means heating is _enabled_, not that the element is drawing power — which is
  why the integration compares the two temperatures rather than trusting it.
- No `E32` or any other `E`-code is reported at temperature, and none of the
  `system_err*`, `earth` or `error` attributes appear at all. Those are
  Bestway heritage; see the note against the Errors sensor in the issue
  tracker.

## Protocol support

| Protocol | Supported          |
| -------- | ------------------ |
| standard | :white_check_mark: |
