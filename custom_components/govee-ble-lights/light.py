from __future__ import annotations

import logging


from homeassistant.components import bluetooth
from homeassistant.components.light import (ATTR_BRIGHTNESS, ATTR_RGB_COLOR, ATTR_EFFECT, ColorMode, LightEntity,
                                            LightEntityFeature, ATTR_COLOR_TEMP_KELVIN)

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.storage import Store
import homeassistant.util.color as color_util

from .const import DOMAIN
from pathlib import Path
import json
import asyncio
import base64

import voluptuous as vol
from bleak.exc import BleakError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo

from homeassistant.exceptions import HomeAssistantError

from .device import GoveeBLEDevice, GoveeCommandRejected
from . import Hub
from datetime import timedelta

SCAN_INTERVAL = timedelta(seconds=30)


_LOGGER = logging.getLogger(__name__)




async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    if config_entry.entry_id in hass.data[DOMAIN]:
        hub: Hub = hass.data[DOMAIN][config_entry.entry_id]
    else:
        return

    if hub.devices is not None:
        devices = hub.devices
        for device in devices:
            if device['type'] == 'devices.types.light':
                _LOGGER.info("Adding device: %s", device)
                async_add_entities([GoveeAPILight(hub, device)])
    elif hub.address is not None:
        async_add_entities([GoveeBluetoothLight(hub, hub.ble, config_entry)], update_before_add=False)
        platform = entity_platform.async_get_current_platform()
        platform.async_register_entity_service(
            "set_segments",
            {
                vol.Required("segments"): vol.All(cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=1, max=16))]),
                vol.Exclusive("rgb_color", "colour"): vol.All(vol.ExactSequence((cv.byte, cv.byte, cv.byte)), vol.Coerce(tuple)),
                vol.Exclusive("color_temp_kelvin", "colour"): vol.All(vol.Coerce(int), vol.Range(min=2000, max=9000)),
                vol.Optional("brightness_pct"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
            },
            "async_set_segments",
        )


class GoveeAPILight(LightEntity, dict):
    _attr_color_mode = ColorMode.RGB

    def __init__(self, hub: Hub, device: dict) -> None:
        """Initialize an API light."""
        super().__init__()

        self.hub = hub

        self._state = None
        self._brightness = None

        self.device_data = device
        self.sku = self.device_data["sku"]
        self.device = self.device_data["device"]

        self._attr_name = device["deviceName"]

        color_modes: set[ColorMode] = set()

        for cap in device["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                color_modes.add(ColorMode.ONOFF)
            if cap['instance'] == 'brightness':
                color_modes.add(ColorMode.BRIGHTNESS)
            if cap['instance'] == 'colorTemperatureK':
                color_modes.add(ColorMode.COLOR_TEMP)
                self._attr_min_color_temp_kelvin = cap['parameters']['range']['min']
                self._attr_max_color_temp_kelvin = cap['parameters']['range']['max']
                self._attr_min_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_min_color_temp_kelvin)
                self._attr_max_mireds = color_util.color_temperature_kelvin_to_mired(self._attr_max_color_temp_kelvin)
            if cap['instance'] == 'colorRgb':
                color_modes.add(ColorMode.RGB)
            if cap['instance'] == 'lightScene':
                self._attr_supported_features = LightEntityFeature(
                    LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
                )

        if ColorMode.ONOFF in color_modes:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
        if ColorMode.BRIGHTNESS in color_modes:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        if ColorMode.COLOR_TEMP in color_modes:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        if ColorMode.RGB in color_modes:
            self._attr_supported_color_modes = {ColorMode.RGB}

        self._state = None
        self._brightness = None
        self.update_scenes()

    async def async_update(self):
        """Retrieve latest state."""
        _LOGGER.info("Updating device: %s", self.device_data)

        state = await self.hub.api.get_device_state(self.sku, self.device)
        for cap in state["capabilities"]:
            if cap['instance'] == 'powerSwitch':
                self._state = cap['state']['value'] == 1
            if cap['instance'] == 'brightness':
                self._brightness = cap['state']['value']
            if cap['instance'] == 'colorTemperatureK':
                value = cap['state']['value']
                if value != 0:
                    self._attr_color_temp_kelvin = value
                    self._attr_color_temp = color_util.color_temperature_kelvin_to_mired(value)
            if cap['instance'] == 'colorRgb':
                num = cap['state']['value']
                self._attr_rgb_color = ((num >> 16) & 0xFF, (num >> 8) & 0xFF, num & 0xFF)

    async def update_scenes(self):
        if LightEntityFeature.EFFECT in self.supported_features:
            if self._attr_effect_list is None or len(self._attr_effect_list) == 0:
                _LOGGER.info("Updating device effects: %s", self.device_data)

                store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
                scenes = await self.hub.api.list_scenes(self.sku, self.device)

                await store.async_save(scenes)

                self._attr_effect_list = [scene['name'] for scene in scenes]

    @property
    def name(self) -> str:
        return self._attr_name

    @property
    def unique_id(self) -> str:
        return self.device

    @property
    def brightness(self):
        return self._brightness

    @property
    def is_on(self) -> bool | None:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            await self.hub.api.set_brightness(self.sku, self.device, (brightness / 255) * 100)
            self._brightness = brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            await self.hub.api.set_color_rgb(self.sku, self.device, red, green, blue)

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
            await self.hub.api.set_color_temp(self.sku, self.device, kelvin)

        if ATTR_EFFECT in kwargs:
            effect_name = kwargs.get(ATTR_EFFECT)
            store = Store(self.hass, 1, f"{DOMAIN}/effect_list_{self.sku}.json")
            scenes = (
                scene for scene in await store.async_load()
                if scene['name'] == effect_name
            )
            scene = next(scenes)
            _LOGGER.info("Set scene: %s", scene)
            await self.hub.api.set_scene(self.sku, self.device, scene['value'])

        await self.hub.api.toggle_power(self.sku, self.device, 1)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hub.api.toggle_power(self.sku, self.device, 0)
        self._state = False


def _load_scenes(model: str) -> dict[str, tuple[int, bytes]]:
    """Scene name -> (sceneCode, scenceParam bytes) from the bundled Govee library.

    The scene payload lives in ``lightEffects[].scenceParam`` (``specialEffect``
    is empty for H6072 and many other models).
    """
    path = Path(__file__).parent / "jsons" / f"{model}.json"
    if not path.exists():
        return {}
    scenes: dict[str, tuple[int, bytes]] = {}
    data = json.loads(path.read_text())
    for category in data["data"]["categories"]:
        for scene in category["scenes"]:
            effects = scene["lightEffects"]
            for effect in effects:
                code = effect.get("sceneCode") or scene.get("sceneCode")
                if not code:
                    continue
                name = f"{category['categoryName']} - {scene['sceneName']}"
                if len(effects) > 1 and effect.get("scenceName"):
                    name += f" - {effect['scenceName']}"
                param = base64.b64decode(effect.get("scenceParam") or "")
                scenes.setdefault(name, (int(code), param))
    return scenes


class GoveeBluetoothLight(LightEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = True
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, hub: Hub, device: GoveeBLEDevice, config_entry: ConfigEntry) -> None:
        self.hub = hub
        self._device = device
        self._mac = hub.address
        self._attr_unique_id = self._mac.replace(":", "")
        self._scenes = _load_scenes(device.model)
        self._scene_by_code = {code: name for name, (code, _) in self._scenes.items()}
        self._attr_effect_list = list(self._scenes)
        if device.segmented:
            self._attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
            self._attr_min_color_temp_kelvin = 2000
            self._attr_max_color_temp_kelvin = 9000
        else:
            self._attr_supported_color_modes = {ColorMode.RGB}
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, self._mac)},
            name=config_entry.title,
            manufacturer="Govee",
            model=device.model,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._device.add_listener(self.async_write_ha_state))

    async def async_will_remove_from_hass(self) -> None:
        await self._device.async_stop()

    async def async_update(self) -> None:
        try:
            await self._device.async_update()
        except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
            if self._attr_available:
                _LOGGER.warning("%s: status poll failed: %s", self._device.name, err)
            self._attr_available = False
            return
        self._attr_available = True

    @property
    def is_on(self) -> bool | None:
        return self._device.state.power

    @property
    def brightness(self) -> int | None:
        return self._device.ha_brightness()

    @property
    def color_mode(self) -> ColorMode:
        if ColorMode.COLOR_TEMP in self._attr_supported_color_modes and self._device.state.kelvin:
            return ColorMode.COLOR_TEMP
        return ColorMode.RGB

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._device.state.kelvin

    @property
    def rgb_color(self):
        if self._device.state.kelvin:
            return None
        return self._device.state.uniform_rgb()

    @property
    def effect(self) -> str | None:
        code = self._device.state.scene_code
        return self._scene_by_code.get(code) if code is not None else None

    @property
    def extra_state_attributes(self):
        state = self._device.state
        return {
            "firmware": state.firmware,
            "mode": f"0x{state.mode:02x}" if state.mode is not None else None,
            "scene_code": state.scene_code,
            "color_temp_kelvin": state.kelvin,
            "segments": {
                seg: {"brightness": s.brightness, "rgb": list(s.rgb)}
                for seg, s in sorted(state.segments.items())
            },
        }

    async def async_turn_on(self, **kwargs) -> None:
        try:
            await self._async_turn_on(**kwargs)
        except GoveeCommandRejected as err:
            raise HomeAssistantError(str(err)) from err

    async def _async_turn_on(self, **kwargs) -> None:
        if not self._device.state.power:
            await self._device.async_set_power(True)
        if ATTR_EFFECT in kwargs:
            name = kwargs[ATTR_EFFECT]
            if name not in self._scenes:
                raise ValueError(f"unknown scene: {name}")
            await self._device.async_set_scene(*self._scenes[name])
        if ATTR_RGB_COLOR in kwargs:
            await self._device.async_set_rgb(tuple(kwargs[ATTR_RGB_COLOR]))
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            white = tuple(int(c) for c in color_util.color_temperature_to_rgb(kelvin))
            await self._device.async_set_color_temp(kelvin, white)
        if ATTR_BRIGHTNESS in kwargs:
            await self._device.async_set_brightness(kwargs[ATTR_BRIGHTNESS])

    async def async_turn_off(self, **kwargs) -> None:
        await self._device.async_set_power(False)

    async def async_set_segments(self, segments, rgb_color=None, color_temp_kelvin=None, brightness_pct=None):
        segments = [int(s) for s in segments]
        if rgb_color is not None:
            await self._device.async_set_rgb(tuple(rgb_color), segments)
        if color_temp_kelvin is not None:
            white = tuple(int(c) for c in color_util.color_temperature_to_rgb(color_temp_kelvin))
            await self._device.async_set_color_temp(color_temp_kelvin, white, segments)
        if brightness_pct is not None:
            await self._device.async_set_segment_brightness(brightness_pct, segments)
