"""Media player platform — one entity per Spotify Connect device the account sees."""

from __future__ import annotations

import asyncio
from collections import deque
import functools
import logging
import time
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import spotify_api
from .const import DOMAIN
from .coordinator import SpotifyLitheCoordinator

_LOGGER = logging.getLogger(__name__)

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
        # instrumentation
        self._last_action: str | None = None
        self._last_action_ms: float | None = None  # API command round-trip
        self._last_action_at: str | None = None
        self._playback_start_ms: float | None = None  # command issued -> audio playing
        self._recent: deque = deque(maxlen=8)  # rolling audit of recent commands
        self._start_task: asyncio.Task | None = None

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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Latency + audit, visible in the UI and recorded to history."""
        return {
            "last_action": self._last_action,
            "last_action_ms": self._last_action_ms,        # API command round-trip
            "last_action_at": self._last_action_at,
            "playback_start_ms": self._playback_start_ms,  # command -> audio playing
            "poll_ms": self.coordinator.last_poll_ms,       # last status-poll latency
            "recent_actions": list(self._recent),
            "device_id": self._device_id,
        }

    async def _call(self, label: str, fn, *args: Any, **kwargs: Any) -> None:
        """Issue a Web API command, recording its round-trip latency + an audit entry."""
        start = time.monotonic()
        ok = True
        try:
            token = await self.coordinator.access_token()
            await self.hass.async_add_executor_job(functools.partial(fn, token, *args, **kwargs))
        except Exception as err:  # noqa: BLE001 - record failure then re-raise
            ok = False
            _LOGGER.warning("%s on %s failed: %s", label, self._attr_name, err)
            raise
        finally:
            ms = round((time.monotonic() - start) * 1000, 1)
            at = dt_util.utcnow().isoformat()
            self._last_action = label
            self._last_action_ms = ms
            self._last_action_at = at
            self._recent.appendleft({"action": label, "ms": ms, "at": at, "ok": ok})
            _LOGGER.debug("%s on %s: %s ms (ok=%s)", label, self._attr_name, ms, ok)
            self.hass.bus.async_fire(
                f"{DOMAIN}_command",
                {
                    "entity_id": self.entity_id,
                    "device": self._attr_name,
                    "action": label,
                    "latency_ms": ms,
                    "success": ok,
                },
            )
            self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def _start_playback(self, label: str, **play_kwargs: Any) -> None:
        """Issue a play command and (in the background) time until audio starts."""
        issued = time.monotonic()
        await self._call(label, spotify_api.play, device_id=self._device_id, **play_kwargs)
        if self._start_task and not self._start_task.done():
            self._start_task.cancel()  # supersede any in-flight measurement
        self._start_task = self.hass.async_create_task(self._await_playback_start(issued))

    async def _await_playback_start(self, issued: float, timeout: float = 15.0) -> None:
        """Poll until this device is actually playing; record the elapsed time."""
        deadline = issued + timeout
        try:
            while time.monotonic() < deadline:
                token = await self.coordinator.access_token()
                pb = await self.hass.async_add_executor_job(spotify_api.current_playback, token)
                dev = pb.get("device") or {}
                if (
                    dev.get("id") == self._device_id
                    and pb.get("is_playing")
                    and (pb.get("progress_ms") or 0) > 0
                ):
                    self._playback_start_ms = round((time.monotonic() - issued) * 1000, 1)
                    _LOGGER.debug(
                        "playback started on %s after %s ms", self._attr_name, self._playback_start_ms
                    )
                    self.async_write_ha_state()
                    return
                await asyncio.sleep(0.4)
            self._playback_start_ms = None  # didn't confirm start within the window
            _LOGGER.debug("playback did not confirm start on %s within %ss", self._attr_name, timeout)
            self.async_write_ha_state()
        except asyncio.CancelledError:
            pass  # superseded by a newer play command
        except spotify_api.SpotifyAPIError as err:
            _LOGGER.debug("playback-start tracking error on %s: %s", self._attr_name, err)

    async def async_media_play(self) -> None:
        await self._start_playback("play")

    async def async_media_pause(self) -> None:
        await self._call("pause", spotify_api.pause, self._device_id)

    async def async_media_next_track(self) -> None:
        await self._call("next", spotify_api.next_track, self._device_id)

    async def async_media_previous_track(self) -> None:
        await self._call("previous", spotify_api.previous_track, self._device_id)

    async def async_set_volume_level(self, volume: float) -> None:
        await self._call("volume", spotify_api.set_volume, int(volume * 100), self._device_id)

    async def async_select_source(self, source: str) -> None:
        uri = next(
            (p["uri"] for p in self.coordinator.data.get("playlists", []) if p["name"] == source),
            None,
        )
        if uri:
            await self._start_playback(f"select_source:{source}", context_uri=uri)
