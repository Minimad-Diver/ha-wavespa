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

This integration began as a fork of the excellent [ha-bestway](https://github.com/cdpuk/ha-bestway) integration by [@cdpuk](https://github.com/cdpuk), by way of [@GraemeDBlue](https://github.com/GraemeDBlue)'s [ha-wavespa](https://github.com/GraemeDBlue/ha-wavespa), adapted to work with the Wavespa cloud API. Full credit and thanks go to their authors, and to the various HA forum posts that provided pointers while reverse engineering this.

It has since diverged considerably. The API client now has its own package, a real-time WebSocket feed replaces polling as the main source of state, and there is support for the Gizwits LAN protocol, so the spa can be read over your own network (see the acknowledgements for the prior work that made that possible). See [Project structure](#project-structure) if you are coming from one of the parent projects.

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

### Beta versions

New features are published as pre-releases before they reach a stable version. To try one, open the integration in HACS, enable beta versions from its overflow menu, then redownload and pick the version you want. You will be offered every subsequent pre-release until you turn that off again.

Betas are cut from the development branch and may not have been through as much real-world use. If you would rather not find out, stay on the latest stable release.

## Configuration

Ensure you can control your device using the Wavespa mobile app.

- Go to **Configuration** > **Devices & Services** > **Add Integration**, then find **WaveSpa** in the list.
- Enter your Wavespa username and password when prompted.

If you picked the wrong API location, or your password changes, use
**Reconfigure** on the integration rather than deleting and re-adding it —
re-adding loses your device and entity history.

## Local access

By default the integration talks to your spa through the Wavespa cloud. It can
also read the spa's state **directly over your local network**, which is
faster and keeps working during a cloud or internet outage.

This is optional and off unless you turn it on. To enable it, open
**Configure** on the integration and enter your spa's IP address.

Two things to know before you do:

- **Only reading is local.** Commands — turning on the bubbles, changing the
  target temperature — are still sent through the cloud. Local writing is
  [tracked separately](https://github.com/Minimad-Diver/ha-wavespa/issues/59),
  and is being taken slowly because a mistake there moves real hardware.
- **Give the spa a fixed address** in your router (a DHCP reservation). If its
  address changes, local access stops working until you update it here.

Once enabled, state changes made from the mobile app or the spa's own panel
appear in Home Assistant within a second or two, because the spa announces
them rather than waiting to be asked. Cloud polling continues in the
background as a safety net.

If anything goes wrong — a wrong address, an unreachable spa, a model whose
data cannot be decoded — local access is skipped and the integration carries
on through the cloud exactly as before. It never costs you your entities.

Only accounts with a single spa can use local access at present. With more
than one, a single address cannot be attributed to a particular spa, and
guessing would show one spa's readings against another.

### Finding the address

If you do not know your spa's IP address, tick **Search my network for the
spa** and press Submit. The integration broadcasts on your network, and fills
the address in for you.

**This cannot work if Home Assistant runs in a Docker container with bridge
networking**, which is a very common setup. The broadcast never leaves the
container, so nothing is ever found. Local access itself is unaffected — just
enter the address by hand, which works normally from a container. Running the
container with host networking restores the search.

Guest networks, AP isolation and some mesh systems also block this kind of
broadcast.

## Entities

Each spa gets:

| Entity                | Type          | Notes                                          |
| --------------------- | ------------- | ---------------------------------------------- |
| Thermostat            | climate       | Target temperature, and heating on/off         |
| Filter                | switch        | Filter pump. Turning it off also stops heating |
| Bubbles               | switch        | Independent of the heater and pump             |
| Alerts                | binary sensor | On when the spa reports a fault                |
| Active alert          | sensor        | Which fault, by name                           |
| Connected             | binary sensor | The cloud API's view of the spa (diagnostic)   |
| Filter life           | sensor        | Remaining filter life, as a percentage         |
| Filter time remaining | sensor        | The same, as filtering time (diagnostic)       |
| Estimated power       | sensor        | See below                                      |
| Estimated energy      | sensor        | See below                                      |
| Protocol version      | sensor        | Gizwits protocol version (diagnostic)          |

Firmware versions are reported on the device itself rather than as separate
sensors — see the device page in Home Assistant.

The **Alerts** sensor reports the three fault flags the manufacturer defines
for this product: filter overdue, overheating, and undercooling. It is
deliberately limited to those, rather than guessing at error codes the
hardware has never been observed to send. **Active alert** says which of them
is raised, in words, because a binary sensor can only say "Problem".

The commonest of the three by far is the filter one, and it clears only at the
spa: press the Filter button on the spa's own control panel. Neither the app
nor this integration can reset the counter, and the spa refuses to be
controlled remotely until it has been.

**Filter time remaining** counts the same thing as **Filter life** but in
hours of _filtering_ rather than percent. It is not a countdown to a date: the
spa's counter only advances while the pump runs, so a spa filtering eight
hours a day takes about three weeks to spend a filter.

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
  temperature**. `Heater = 1` on its own only means heating is _enabled_; the
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

For local access problems, the diagnostics carry two useful fields:
`lan_connected` says whether a session is established, and `last_lan_update`
says when the spa last sent anything over it. A session that is connected but
has never delivered is a different problem from one that will not connect.

`null` for `lan_connected` simply means local access is switched off.

To capture logs, add this to `configuration.yaml` and restart:

```yaml
logger:
  logs:
    custom_components.wavespa: debug
```

## Project structure

Useful if you are coming from one of the parent projects, or working on this
one:

| Path                                 | What it holds                                                                                      |
| ------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `custom_components/wavespa/`         | The Home Assistant integration: platforms, config flow, coordinator                                |
| `custom_components/wavespa/wavespa/` | The cloud client — REST API, WebSocket feed, and the data model. No Home Assistant imports         |
| `custom_components/wavespa/lan/`     | The Gizwits LAN protocol — framing, datapoint codec, session, discovery. No Home Assistant imports |
| `scripts/`                           | Research tools, not shipped behaviour                                                              |
| `tests/`                             | The test suite                                                                                     |

The `wavespa/` and `lan/` packages deliberately know nothing about Home
Assistant, so both can be exercised without it.

The LAN payload layout is **not hardcoded**. It comes from the product's own
datapoint definition, fetched at runtime from the Gizwits API using the
`product_key` your account reports, so a new spa model needs no code change.

## Acknowledgements

Built on the work of others:

- [@GraemeDBlue/ha-wavespa](https://github.com/GraemeDBlue/ha-wavespa) — the fork this one continues from
- [@cdpuk/ha-bestway](https://github.com/cdpuk/ha-bestway) — the original integration both descend from
- [@B-Hartley/bruces_homeassistant_config](https://github.com/B-Hartley/bruces_homeassistant_config) — reference configuration

The LAN protocol support draws on:

- [chrisc123/jebao_aqua-homeassistant](https://github.com/chrisc123/jebao_aqua-homeassistant) — a Gizwits LAN implementation for a different product, MIT licensed, from which the transport framing was derived
- [gizwits/Gizwits-GAgent](https://github.com/gizwits/Gizwits-GAgent) — the vendor's own module firmware, which is the authority on the command table and the connection handling
- [Apollon77/node-ph803w](https://github.com/Apollon77/node-ph803w/blob/main/PROTOCOL.md) — a written specification of the same protocol, independently confirming the framing and command codes

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
