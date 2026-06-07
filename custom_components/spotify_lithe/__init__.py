"""The Spotify Lithe integration — Spotify Connect control as media_player entities."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .coordinator import SpotifyLitheCoordinator

PLATFORMS = [Platform.MEDIA_PLAYER]

type SpotifyLitheConfigEntry = ConfigEntry[SpotifyLitheCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SpotifyLitheConfigEntry) -> bool:
    """Set up Spotify Lithe from a config entry."""
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(hass, entry)
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    coordinator = SpotifyLitheCoordinator(hass, session)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpotifyLitheConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
