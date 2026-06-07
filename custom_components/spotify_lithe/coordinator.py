"""Polls Spotify playback state, devices and playlists for the account."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import spotify_api
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class SpotifyLitheCoordinator(DataUpdateCoordinator[dict]):
    """Coordinator: one Spotify account → playback + devices + playlists."""

    def __init__(
        self, hass: HomeAssistant, session: config_entry_oauth2_flow.OAuth2Session
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.session = session
        self._playlists: list | None = None

    async def access_token(self) -> str:
        """A valid access token (refreshed by HA's OAuth helper as needed)."""
        await self.session.async_ensure_token_valid()
        return self.session.token["access_token"]

    async def _async_update_data(self) -> dict:
        token = await self.access_token()
        try:
            playback = await self.hass.async_add_executor_job(
                spotify_api.current_playback, token
            )
            devices = await self.hass.async_add_executor_job(spotify_api.devices, token)
            if self._playlists is None:  # playlists change rarely; fetch once, cache
                self._playlists = await self.hass.async_add_executor_job(
                    spotify_api.playlists, token
                )
        except spotify_api.SpotifyAPIError as err:
            raise UpdateFailed(str(err)) from err
        return {
            "playback": playback,
            "devices": devices,
            "playlists": self._playlists or [],
        }
