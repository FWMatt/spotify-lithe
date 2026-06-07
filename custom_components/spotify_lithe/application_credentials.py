"""Application Credentials platform — lets users supply a Spotify app id/secret."""

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

from .const import OAUTH2_AUTHORIZE, OAUTH2_TOKEN


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    return AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN)


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    return {
        "developer_console_url": "https://developer.spotify.com/dashboard",
        "redirect_url": "https://my.home-assistant.io/redirect/oauth",
    }
