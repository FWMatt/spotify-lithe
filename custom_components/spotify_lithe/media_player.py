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

OPTIMISTIC_TTL = 8.0          # seconds an override is trusted before reality must win
REPOLL_DELAYS = (1.0, 3.0)    # extra refreshes after a command, to outrun Spotify lag
# A pause->resume within this window collapses to the final intent (no device pause/resume
# round-trip). Set wide because resume-from-pause can starve the LWF1's audio pipeline
# (cloud reports playing + progress, but no audio frames reach the speaker -> silence).
# The optimistic overlay flips the card instantly, so this delay isn't visible in the UI.
TOGGLE_DEBOUNCE = 1.2         # coalesce play<->pause toggles before hitting the device
MIN_CMD_GAP = 0.3             # minimum spacing between device commands (protects a fragile LWF1)


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
        # diagnostics surfaced as entity attributes (no debug logging needed to see them)
        self._last_error: str | None = None
        self._last_error_at: str | None = None
        self._last_start_ok: bool | None = None  # did the most recent play actually start?
        # optimistic overlay: the intended outcome shown instantly, reconciled on poll
        self._opt: dict | None = None  # {playing, active, volume, expires}
        self._repoll_tasks: list[asyncio.Task] = []
        # command pacing: serialise + space device commands; coalesce play/pause toggles
        self._cmd_lock = asyncio.Lock()
        self._last_cmd_mono = 0.0
        self._toggle_gen = 0
        self._desired_play: bool | None = None

    @callback
    def _set_optimistic(
        self,
        *,
        playing: bool | None = None,
        active: bool | None = None,
        volume: float | None = None,
    ) -> None:
        """Overlay the intended outcome for a short window, then push state at once."""
        self._opt = {
            "playing": playing,
            "active": active,
            "volume": volume,
            "expires": time.monotonic() + OPTIMISTIC_TTL,
        }
        self.async_write_ha_state()

    @property
    def _optimistic(self) -> dict | None:
        """The active override, or None if absent/expired (reading clears on timeout)."""
        opt = self._opt
        if opt is None:
            return None
        if time.monotonic() >= opt["expires"]:
            self._opt = None
            return None
        return opt

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
        opt = self._optimistic
        if opt is not None and opt["active"] is not None:
            return opt["active"]
        return (self._playback.get("device") or {}).get("id") == self._device_id

    @property
    def state(self) -> MediaPlayerState:
        if not self._is_active:
            return MediaPlayerState.IDLE
        opt = self._optimistic
        if opt is not None and opt["playing"] is not None:
            return MediaPlayerState.PLAYING if opt["playing"] else MediaPlayerState.PAUSED
        return (
            MediaPlayerState.PLAYING
            if self._playback.get("is_playing")
            else MediaPlayerState.PAUSED
        )

    @property
    def volume_level(self) -> float | None:
        opt = self._optimistic
        if opt is not None and opt["volume"] is not None:
            return opt["volume"]
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
            "last_action_ok": self._recent[0]["ok"] if self._recent else None,
            "playback_start_ms": self._playback_start_ms,  # command -> audio playing
            "last_start_ok": self._last_start_ok,           # None=pending, False=stalled/no start
            "poll_ms": self.coordinator.last_poll_ms,       # last status-poll latency
            "poll_ok": self.coordinator.last_update_success,
            "poll_error": (
                None if self.coordinator.last_update_success
                else str(self.coordinator.last_exception)
            ),
            "error": self._last_error,                      # most recent command failure
            "error_at": self._last_error_at,
            "recent_actions": list(self._recent),
            "device_id": self._device_id,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Clear the optimistic overlay once a poll confirms reality matches it."""
        opt = self._optimistic  # also clears if expired
        if opt is not None and self._reality_matches(opt):
            self._opt = None
        super()._handle_coordinator_update()

    @callback
    def _reality_matches(self, opt: dict) -> bool:
        """True once coordinator data already reflects the intended outcome."""
        real_active = (self._playback.get("device") or {}).get("id") == self._device_id
        if opt["active"] is not None and opt["active"] != real_active:
            return False
        if opt["playing"] is not None:
            if not real_active:
                return False
            if bool(self._playback.get("is_playing")) != opt["playing"]:
                return False
        if opt["volume"] is not None:
            dev = self._device
            real_vol = dev.get("volume_percent") if dev else None
            if real_vol is None or round(real_vol / 100, 2) != round(opt["volume"], 2):
                return False
        return True

    def _schedule_repolls(self) -> None:
        """Delayed refreshes so Spotify's eventual consistency catches up < the 10s poll."""
        for t in self._repoll_tasks:
            if not t.done():
                t.cancel()
        self._repoll_tasks = [
            self.hass.async_create_task(self._delayed_refresh(d)) for d in REPOLL_DELAYS
        ]

    async def _delayed_refresh(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self.coordinator.async_request_refresh()
        except asyncio.CancelledError:
            pass

    async def _call(self, label: str, fn, *args: Any, **kwargs: Any) -> None:
        """Issue a Web API command, recording its round-trip latency + an audit entry.

        Commands are serialised through a per-entity lock and spaced by at least
        MIN_CMD_GAP, so a burst (rapid pause/play/skip) can't pile onto the fragile
        LWF1 Connect client all at once.
        """
        async with self._cmd_lock:
            gap = time.monotonic() - self._last_cmd_mono
            if gap < MIN_CMD_GAP:
                await asyncio.sleep(MIN_CMD_GAP - gap)
            start = time.monotonic()
            ok = True
            try:
                token = await self.coordinator.access_token()
                await self.hass.async_add_executor_job(functools.partial(fn, token, *args, **kwargs))
            except Exception as err:  # noqa: BLE001 - record failure then re-raise
                ok = False
                self._last_error = f"{label}: {err}"
                self._last_error_at = dt_util.utcnow().isoformat()
                _LOGGER.warning("%s on %s failed: %s", label, self._attr_name, err)
                raise
            finally:
                self._last_cmd_mono = time.monotonic()
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
            self._schedule_repolls()

    async def _start_playback(self, label: str, **play_kwargs: Any) -> None:
        """Issue a play command and (in the background) time until audio starts."""
        issued = time.monotonic()
        await self._call(label, spotify_api.play, device_id=self._device_id, **play_kwargs)
        if self._start_task and not self._start_task.done():
            self._start_task.cancel()  # supersede any in-flight measurement
        self._start_task = self.hass.async_create_task(self._await_playback_start(issued))

    async def _await_playback_start(self, issued: float, timeout: float = 15.0) -> None:
        """Poll until this device is actually playing; record the elapsed time.

        A timeout here is the headline "I pressed play and nothing happened" signal
        (e.g. a stalled Connect session) — surfaced via the `last_start_ok` attribute
        and a warning, not just a debug line.
        """
        deadline = issued + timeout
        delay = 0.5  # back off so we don't hammer the Web API for the full window
        self._last_start_ok = None  # pending
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
                    self._last_start_ok = True
                    _LOGGER.debug(
                        "playback started on %s after %s ms", self._attr_name, self._playback_start_ms
                    )
                    self.async_write_ha_state()
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 1.4, 2.5)
            self._playback_start_ms = None  # didn't confirm start within the window
            self._last_start_ok = False
            _LOGGER.warning(
                "%s: playback did not start within %ss after command — device may have "
                "stalled (active=%s, is_playing=%s)",
                self._attr_name,
                timeout,
                (self._playback.get("device") or {}).get("id") == self._device_id,
                self._playback.get("is_playing"),
            )
            self.async_write_ha_state()
        except asyncio.CancelledError:
            pass  # superseded by a newer play command
        except spotify_api.SpotifyAPIError as err:
            self._last_error = f"playback-start-poll: {err}"
            self._last_error_at = dt_util.utcnow().isoformat()
            _LOGGER.warning("playback-start tracking error on %s: %s", self._attr_name, err)

    @callback
    def _clear_optimistic(self) -> None:
        """Drop the overlay (on command failure) so a bad command never sticks."""
        self._opt = None
        self.async_write_ha_state()

    async def async_media_play(self) -> None:
        self._set_optimistic(playing=True, active=True)
        await self._debounced_toggle(True)

    async def async_media_pause(self) -> None:
        self._set_optimistic(playing=False)  # active unchanged: stays this device
        await self._debounced_toggle(False)

    async def _debounced_toggle(self, play: bool) -> None:
        """Coalesce a rapid play/pause burst: the card flips instantly (optimistic),
        but only the *latest* intent, after a short quiet window, reaches the device."""
        self._desired_play = play
        self._toggle_gen += 1
        gen = self._toggle_gen
        try:
            await asyncio.sleep(TOGGLE_DEBOUNCE)
        except asyncio.CancelledError:
            return
        if gen != self._toggle_gen:
            return  # a newer press superseded this one; it will dispatch the final state
        try:
            if self._desired_play:
                await self._start_playback("play")
            else:
                await self._call("pause", spotify_api.pause, self._device_id)
        except Exception:
            self._clear_optimistic()
            raise

    async def async_media_next_track(self) -> None:
        self._set_optimistic(playing=True, active=True)
        try:
            await self._call("next", spotify_api.next_track, self._device_id)
        except Exception:
            self._clear_optimistic()
            raise

    async def async_media_previous_track(self) -> None:
        self._set_optimistic(playing=True, active=True)
        try:
            await self._call("previous", spotify_api.previous_track, self._device_id)
        except Exception:
            self._clear_optimistic()
            raise

    async def async_set_volume_level(self, volume: float) -> None:
        self._set_optimistic(volume=volume)
        try:
            await self._call("volume", spotify_api.set_volume, int(volume * 100), self._device_id)
        except Exception:
            self._clear_optimistic()
            raise

    async def async_select_source(self, source: str) -> None:
        uri = next(
            (p["uri"] for p in self.coordinator.data.get("playlists", []) if p["name"] == source),
            None,
        )
        if uri:
            self._set_optimistic(playing=True, active=True)
            try:
                await self._start_playback(f"select_source:{source}", context_uri=uri)
            except Exception:
                self._clear_optimistic()
                raise

    async def async_will_remove_from_hass(self) -> None:
        for t in (*self._repoll_tasks, self._start_task):
            if t and not t.done():
                t.cancel()
        await super().async_will_remove_from_hass()
