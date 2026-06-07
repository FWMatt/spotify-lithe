"""Config flow for Spotify Lithe — OAuth2 via Application Credentials.

One config entry == one Spotify account (uniqued by the Spotify user id), which
is the seam for the future multi-account / per-room-simultaneous-streams setup.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, SCOPES

_LOGGER = logging.getLogger(__name__)


class SpotifyLitheOAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle the OAuth2 config flow for Spotify Lithe."""

    DOMAIN = DOMAIN

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {"scope": " ".join(SCOPES)}

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> Any:
        """Title the entry by the Spotify account, unique per Spotify user id."""
        name = "Spotify Lithe"
        try:
            session = async_get_clientsession(self.hass)
            resp = await session.get(
                "https://api.spotify.com/v1/me",
                headers={"Authorization": f"Bearer {data['token']['access_token']}"},
            )
            if resp.status == 200:
                me = await resp.json()
                if me.get("id"):
                    await self.async_set_unique_id(me["id"])
                name = me.get("display_name") or me.get("id") or name
        except Exception:  # noqa: BLE001 - profile lookup is best-effort for titling
            _LOGGER.debug("Could not fetch Spotify profile for entry title", exc_info=True)

        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=name, data=data)
