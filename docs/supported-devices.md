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

| Model          | Reported `product_name` | `product_key`                      | Protocol |
| -------------- | ----------------------- | ---------------------------------- | -------- |
| Wave Spa Garda | `Wave_SPA_EU`           | `747be354e00449e799883a966d0c9cbd` | standard |

The `product_key` identifies the model rather than the individual spa, so it is
safe to quote in an issue. It is the lookup key for the datapoint definition
described below.

Note the manufacturer's own definition for that key names the product
`Wave_SPA_UL`, while the bindings API reports `product_name` as `Wave_SPA_EU`.
Same product, two different labels; the integration goes by what the API
reports.

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
| `Overtime_filter`       | `0`     | Alerts sensor                   |
| `Superheat`             | `0`     | Alerts sensor                   |
| `Undercooling`          | `0`     | Alerts sensor                   |
| `Fp`                    | `0`     | Not used - meaning unknown      |
| `Temp1` `Temp2` `Temp3` | `0`     | Not used - meaning unknown      |
| `bit1` `bit2`           | `0`     | Not used - meaning unknown      |

Three things worth knowing about this sample:

- `Heater` is `1` while the water is 26 °C against a 24 °C target. The flag
  means heating is _enabled_, not that the element is drawing power — which is
  why the integration compares the two temperatures rather than trusting it.
- `Time_filter` counts **up** as the filter is used, towards a maximum of
  10200. The unit is minutes, measured against a live spa at one per 65
  seconds of filtering, so a full filter life is about 170 hours. The Filter
  life sensor reports the inverse as a percentage.
- No `E32` or any other `E`-code is reported, and none of the `system_err*`,
  `earth` or `error` attributes appear at all. Those are Bestway heritage and
  this hardware has never been observed to send them, which is why the Alerts
  sensor reads the three attributes above instead of searching for error
  codes.

## Datapoint layout

The manufacturer publishes a definition for each `product_key`, giving every
datapoint a name, a type and a position. The integration fetches it at runtime
rather than bundling it, so **a new model needs no code change to be decoded** —
only its `product_name` added above, so the thermostat and switches are
offered.

For `747be354e00449e799883a966d0c9cbd` the status payload is 9 bytes:

| Byte | Bits | Datapoint               | Type                         |
| ---- | ---- | ----------------------- | ---------------------------- |
| 0    | 0    | `Heater`                | `status_writable`            |
| 0    | 1    | `Bubble`                | `status_writable`            |
| 0    | 2    | `Filter`                | `status_writable`            |
| 0    | 3    | `Fp`                    | `status_writable`            |
| 0    | 4    | `bit1`                  | `status_writable`            |
| 0    | 5    | `bit2`                  | `status_writable`            |
| 1    | -    | `Temperature_setup`     | `status_writable`            |
| 2-4  | -    | `Temp1` `Temp2` `Temp3` | `status_writable`            |
| 5    | -    | `Current_temperature`   | `status_readonly`            |
| 6-7  | -    | `Time_filter`           | `status_readonly`, max 10200 |
| 8    | 0    | `Overtime_filter`       | `alert`                      |
| 8    | 1    | `Superheat`             | `alert`                      |
| 8    | 2    | `Undercooling`          | `alert`                      |

Two consequences of the manufacturer's own typing:

- `Overtime_filter`, `Superheat` and `Undercooling` are typed `alert`, not
  guessed at. That is the evidence the Alerts sensor is built on.
- `Fp`, `Temp1`-`Temp3`, `bit1` and `bit2` are `status_writable`, so they are
  probably controls the app does not expose. Nobody has established what they
  do, and they are not written speculatively — a wrong guess here moves real
  hardware.

## Protocol support

| Protocol         | Supported          | Notes                                                         |
| ---------------- | ------------------ | ------------------------------------------------------------- |
| standard (cloud) | :white_check_mark: | REST polling plus a WebSocket feed for real-time updates      |
| Gizwits LAN      | :white_check_mark: | Reading only, opt-in. See the README for setup and its limits |

Local writing over the LAN is not implemented; commands go via the cloud. The
spa announces state changes over the LAN unprompted, so local access is
push-driven rather than polled.
