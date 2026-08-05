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