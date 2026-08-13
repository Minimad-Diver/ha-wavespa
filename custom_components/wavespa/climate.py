"""Climate platform support."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
from homeassistant.components.climate.const import ATTR_HVAC_MODE, HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import WavespaConfigEntry, WavespaUpdateCoordinator
from .wavespa.model import WavespaDeviceType, as_int
from .entity import WavespaEntity

_SPA_MIN_TEMP_C = 20
_SPA_MIN_TEMP_F = 68
_SPA_MAX_TEMP_C = 40
_SPA_MAX_TEMP_F = 104
_CLIMATE_FEATURES = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TURN_OFF
    | ClimateEntityFeature.TURN_ON
)


# Entity state comes from the coordinator, so updates are not per-entity
# polling and do not need serialising.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: WavespaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up climate entities."""
    coordinator = config_entry.runtime_data

    entities: list[WavespaEntity] = []

    for device_id, device in coordinator.api.devices.items():
        if device.device_type in [
            WavespaDeviceType.WAVESPA_EU,
            WavespaDeviceType.WAVESPA_US,
        ]:
            entities.append(WaveSpaThermostat(coordinator, config_entry, device_id))

    async_add_entities(entities)


class WaveSpaThermostat(WavespaEntity, ClimateEntity):
    """A thermostat for WaveSpa devices."""

    _attr_name = "Thermostat"
    _attr_supported_features = _CLIMATE_FEATURES
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_precision = PRECISION_WHOLE
    _attr_target_temperature_step = 1

    def __init__(
        self,
        coordinator: WavespaUpdateCoordinator,
        config_entry: WavespaConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize thermostat."""
        super().__init__(coordinator, config_entry, device_id)
        self._attr_unique_id = f"{device_id}_thermostat"

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return the current mode (HEAT or OFF)."""
        if not self.status:
            return None
        heater = self.status.flag("Heater")
        if heater is None:
            return None
        return HVACMode.HEAT if heater else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running action (OFF, HEATING or IDLE)."""
        if not self.status:
            return None

        # Heating switched off entirely is OFF, matching hvac_mode. IDLE is
        # reserved for "on, but not currently calling for heat".
        heater = self.status.flag("Heater")
        if heater is None:
            return None
        if not heater:
            return HVACAction.OFF

        heating = self.status.is_heating
        if heating is None:
            return None
        return HVACAction.HEATING if heating else HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        if not self.status:
            return None
        return as_int(self.status.attrs.get("Current_temperature"))

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        if not self.status:
            return None
        return as_int(self.status.attrs.get("Temperature_setup"))

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement used by the platform."""
        # Looked up with .get() rather than indexing: a bindings refresh that
        # omits the device would otherwise raise from a property Home Assistant
        # reads routinely.
        device = self.wavespa_device
        if device is not None and device.device_type == WavespaDeviceType.WAVESPA_US:
            return str(UnitOfTemperature.FAHRENHEIT)
        # Default to Celsius for other (and unknown) device types
        return str(UnitOfTemperature.CELSIUS)

    @property
    def min_temp(self) -> float:
        """
        Get the minimum temperature that a user can set.

        As the Spa can be switched between temperature units, this needs to be dynamic.
        """
        return (
            _SPA_MIN_TEMP_C
            if self.temperature_unit == UnitOfTemperature.CELSIUS
            else _SPA_MIN_TEMP_F
        )

    @property
    def max_temp(self) -> float:
        """
        Get the maximum temperature that a user can set.

        As the Spa can be switched between temperature units, this needs to be dynamic.
        """
        return (
            _SPA_MAX_TEMP_C
            if self.temperature_unit == UnitOfTemperature.CELSIUS
            else _SPA_MAX_TEMP_F
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        should_heat = hvac_mode == HVACMode.HEAT
        await self.coordinator.api.spa_set_heat(self.device_id, should_heat)
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a new target temperature."""
        target_temperature = kwargs.get(ATTR_TEMPERATURE)
        if target_temperature is None:
            return

        if hvac_mode := kwargs.get(ATTR_HVAC_MODE):
            should_heat = hvac_mode == HVACMode.HEAT
            await self.coordinator.api.spa_set_heat(self.device_id, should_heat)

        await self.coordinator.api.spa_set_target_temp(
            self.device_id, target_temperature
        )
        await self.coordinator.async_request_refresh()
