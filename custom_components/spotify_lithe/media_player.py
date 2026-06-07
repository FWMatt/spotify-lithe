"""Media player platform — one entity per Spotify Connect device the account sees."""

from __future__ import annotations

import functools
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import spotify_api
from .coordinator import SpotifyLitheCoordinator

SUPPORT = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.SELECT_SOURCE
)


async def async_setup_entry(hass, entry, async_add_entities: AddEntitiesCallback) -> None:
    """Create a media_player per Connect device, adding new ones as they appear."""
    coordinator: SpotifyLitheCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _discover() -> None:
        new = []
        for dev in coordinator.data.get("devices", []):
            did = dev.get("id")
            if did and did not in known:
                known.add(did)
                new.append(
                    SpotifyLitheMediaPlayer(coordinator, entry.entry_id, did, dev.get("name"))
                )
        if new:
            async_add_entities(new)

    _discover()
    entry.async_on_unload(coordinator.async_add_listener(_discover))


class SpotifyLitheMediaPlayer(CoordinatorEntity[SpotifyLitheCoordinator], MediaPlayerEntity):
    """A Spotify Connect device, controlled via the Web API."""

    _attr_supported_features = SUPPORT

    def __init__(self, coordinator, entry_id: str, device_id: str, name: str | None) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_name = name
        self._attr_unique_id = f"{entry_id}:{device_id}"

    @property
    def _device(self) -> dict | None:
        for dev in self.coordinator.data.get("devices", []):
            if dev.get("id") == self._device_id:
                return dev
        return None

    @property
    def available(self) -> bool:
        return super().available and self._device is not None

    @property
    def _playback(self) -> dict:
        return self.coordinator.data.get("playback") or {}

    @property
    def _is_active(self) -> bool:
        # Spotify reports now-playing only for the single active device.
        return (self._playback.get("device") or {}).get("id") == self._device_id

    @property
    def state(self) -> MediaPlayerState:
        if self._is_active:
            return (
                MediaPlayerState.PLAYING
                if self._playback.get("is_playing")
                else MediaPlayerState.PAUSED
            )
        return MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float | None:
        dev = self._device
        vol = dev.get("volume_percent") if dev else None
        return vol / 100 if vol is not None else None

    @property
    def media_title(self) -> str | None:
        if not self._is_active:
            return None
        return (self._playback.get("item") or {}).get("name")

    @property
    def media_artist(self) -> str | None:
        if not self._is_active:
            return None
        artists = (self._playback.get("item") or {}).get("artists") or []
        return ", ".join(a["name"] for a in artists) or None

    @property
    def source_list(self) -> list[str]:
        return [p["name"] for p in self.coordinator.data.get("playlists", [])]

    @property
    def source(self) -> str | None:
        if not self._is_active:
            return None
        uri = (self._playback.get("context") or {}).get("uri")
        for p in self.coordinator.data.get("playlists", []):
            if p["uri"] == uri:
                return p["name"]
        return None

    async def _call(self, fn, *args: Any, **kwargs: Any) -> None:
        token = await self.coordinator.access_token()
        await self.hass.async_add_executor_job(functools.partial(fn, token, *args, **kwargs))
        await self.coordinator.async_request_refresh()

    async def async_media_play(self) -> None:
        await self._call(spotify_api.play, device_id=self._device_id)

    async def async_media_pause(self) -> None:
        await self._call(spotify_api.pause, self._device_id)

    async def async_media_next_track(self) -> None:
        await self._call(spotify_api.next_track, self._device_id)

    async def async_media_previous_track(self) -> None:
        await self._call(spotify_api.previous_track, self._device_id)

    async def async_set_volume_level(self, volume: float) -> None:
        await self._call(spotify_api.set_volume, int(volume * 100), self._device_id)

    async def async_select_source(self, source: str) -> None:
        uri = next(
            (p["uri"] for p in self.coordinator.data.get("playlists", []) if p["name"] == source),
            None,
        )
        if uri:
            await self._call(spotify_api.play, device_id=self._device_id, context_uri=uri)
