# WaveSpa

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)
[![hacs][hacsbadge]][hacs]

This custom component integrates with the Wavespa cloud API, providing control of devices such as WaveSpa Garda hot tubs.

<p float="left">
  <img src="images/demo-thermostat.png" width="200" />
  <img src="images/demo-controls.png" width="200" />
  <img src="images/demo-diagnostic.png" width="200" />
</p>

## Foreword

This integration is a fork of the excellent [ha-bestway](https://github.com/cdpuk/ha-bestway) integration by [@cdpuk](https://github.com/cdpuk), adapted to work with the Wavespa cloud API. Much of the structure originate from that project — full credit and thanks go to its author. Thank you also to the various HA forum posts that provided pointers while reverse engineering this to work.

## Required Account

You must have an account with the Wavespa mobile app.

Wavespa uses different API endpoints for EU and US. If you get an error stating account could not be found, try using the other endpoint. If this does not help, then create a new account under a supported country.

## Device Support

A Wi-Fi enabled model is required. No custom hardware is required.

See the [supported devices](docs/supported-devices.md) list for more details.

## Installation

This integration is delivered as a HACS custom repository.

1. Download and install [HACS][hacs-download].
2. Add a [custom repository][hacs-custom] in HACS. You will need to enter the URL of this repository when prompted: `https://github.com/Minimad-Diver/ha-wavespa`.

## Configuration

Ensure you can control your device using the Wavespa mobile app.

- Go to **Configuration** > **Devices & Services** > **Add Integration**, then find **WaveSpa** in the list.
- Enter your Wavespa username and password when prompted.

If you picked the wrong API location, or your password changes, use
**Reconfigure** on the integration rather than deleting and re-adding it —
re-adding loses your device and entity history.

## Entities

Each spa gets:

| Entity            | Type          | Notes                                          |
| ----------------- | ------------- | ---------------------------------------------- |
| Thermostat        | climate       | Target temperature, and heating on/off         |
| Filter            | switch        | Filter pump. Turning it off also stops heating |
| Bubbles           | switch        | Independent of the heater and pump             |
| Errors            | binary sensor | On when the spa reports a fault                |
| Connected         | binary sensor | The cloud API's view of the spa (diagnostic)   |
| Filter            | sensor        | Remaining filter life, as a percentage         |
| Estimated power   | sensor        | See below                                      |
| Estimated energy  | sensor        | See below                                      |
| Firmware versions | sensors       | MCU and Wi-Fi versions (diagnostic)            |

### Estimated power and energy

**These are modelled, not measured.** The spa has no power meter, so the
integration infers consumption from which loads it reports as running:

| Load        | Assumed draw |
| ----------- | ------------ |
| Heater      | 1800 W       |
| Bubbles     | 600 W        |
| Filter pump | 50 W         |

Two details worth knowing:

- The heater is only counted while the spa is **below its target
  temperature**. `Heater = 1` on its own only means heating is *enabled*; the
  element cycles off once the water is up to temperature, which is where a spa
  spends most of its day.
- If the spa is unreachable, no consumption is recorded for that period rather
  than assuming it carried on at the last known rate.

The defaults suit a typical 13 A EU spa. If yours differs — US models run at a
different voltage — set your own figures under **Configure** on the
integration. `Estimated energy` is a `total_increasing` kWh sensor, so it can
be added to the Energy dashboard; just bear in mind the dashboard will then be
showing an estimate.

## Troubleshooting

Before opening an issue, download diagnostics: on the integration page, use the
three-dot menu next to your device and choose **Download diagnostics**. It
contains the raw values your spa reports, which is usually what's needed to
diagnose a problem, with credentials and identifiers redacted automatically.

## Acknowledgements

- https://github.com/GraemeDBlue/ha-wavespa
- https://github.com/cdpuk/ha-bestway
- https://github.com/B-Hartley/bruces_homeassistant_config

## Contributing

If you want to contribute to this please read the [Contribution Guidelines](CONTRIBUTING.md).

[commits-shield]: https://img.shields.io/github/commit-activity/y/Minimad-Diver/ha-wavespa.svg?style=for-the-badge
[commits]: https://github.com/Minimad-Diver/ha-wavespa/commits/main
[hacs]: https://github.com/custom-components/hacs
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[license-shield]: https://img.shields.io/github/license/Minimad-Diver/ha-wavespa.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/Minimad-Diver/ha-wavespa.svg?style=for-the-badge
[releases]: https://github.com/Minimad-Diver/ha-wavespa/releases
[hacs-download]: https://hacs.xyz/docs/setup/download
[hacs-custom]: https://hacs.xyz/docs/faq/custom_repositories
