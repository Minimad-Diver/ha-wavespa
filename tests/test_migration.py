"""Tests for async_migrate_entry.

Rewritten in d7baa18 to make the steps cumulative, and untested until now.
Migration only ever runs on someone else's oldest install, so a mistake here
surfaces on exactly the entries with the most history behind them.
"""

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wavespa import async_migrate_entry
from custom_components.wavespa.const import (
    CONF_API_ROOT,
    CONF_API_ROOT_EU,
    CONF_API_ROOT_US,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONFIG_VERSION,
    DOMAIN,
)


def _entry(hass, version: int, data: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data
        if data is not None
        else {CONF_USERNAME: "test@example.org", CONF_PASSWORD: "P@asw0rd"},
        version=version,
        entry_id=f"migrate_v{version}",
    )
    entry.add_to_hass(hass)
    return entry


class TestMigrationFromV1:
    """Version 1 predates the EU/US choice and had the endpoint hardcoded."""

    async def test_sets_the_eu_api_root(self, hass) -> None:
        entry = _entry(hass, version=1)

        assert await async_migrate_entry(hass, entry) is True

        assert entry.data[CONF_API_ROOT] == CONF_API_ROOT_EU

    async def test_bumps_the_version(self, hass) -> None:
        entry = _entry(hass, version=1)

        await async_migrate_entry(hass, entry)

        assert entry.version == CONFIG_VERSION

    async def test_existing_credentials_are_preserved(self, hass) -> None:
        entry = _entry(hass, version=1)

        await async_migrate_entry(hass, entry)

        assert entry.data[CONF_USERNAME] == "test@example.org"
        assert entry.data[CONF_PASSWORD] == "P@asw0rd"


class TestMigrationAtCurrentVersion:
    """The case the old implementation got wrong.

    It handled `entry.version == 1` and failed everything else. That was
    unreachable while the current version was 2, but would have rejected every
    existing v2 entry the moment a v3 was added - breaking precisely the
    longest-standing installs.
    """

    async def test_current_version_succeeds(self, hass) -> None:
        entry = _entry(
            hass,
            version=CONFIG_VERSION,
            data={
                CONF_USERNAME: "test@example.org",
                CONF_PASSWORD: "P@asw0rd",
                CONF_API_ROOT: CONF_API_ROOT_US,
            },
        )

        assert await async_migrate_entry(hass, entry) is True

    async def test_current_version_is_left_alone(self, hass) -> None:
        """A US entry must not be quietly reset to the EU endpoint."""
        entry = _entry(
            hass,
            version=CONFIG_VERSION,
            data={
                CONF_USERNAME: "test@example.org",
                CONF_PASSWORD: "P@asw0rd",
                CONF_API_ROOT: CONF_API_ROOT_US,
            },
        )

        with patch.object(hass.config_entries, "async_update_entry") as update:
            await async_migrate_entry(hass, entry)

        update.assert_not_called()
        assert entry.data[CONF_API_ROOT] == CONF_API_ROOT_US


class TestDowngrade:
    """An entry written by a newer version of the integration."""

    async def test_is_refused(self, hass) -> None:
        entry = _entry(hass, version=CONFIG_VERSION + 1)

        assert await async_migrate_entry(hass, entry) is False

    async def test_leaves_the_entry_untouched(self, hass) -> None:
        entry = _entry(hass, version=CONFIG_VERSION + 1)
        before = dict(entry.data)

        await async_migrate_entry(hass, entry)

        assert dict(entry.data) == before
        assert entry.version == CONFIG_VERSION + 1
