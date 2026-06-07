"""Shared, dependency-free Spotify Web API client (stdlib only, synchronous).

This module holds *only* the Web API call logic — no OAuth, no token storage.
Callers pass a valid access token in. It is used by both the `spotify-lithe`
CLI (which manages its own PKCE token) and the Home Assistant integration
(which gets tokens from HA's OAuth helper and runs these calls in an executor).

The important, hard-won behaviour preserved here:
  * play() of a URI is a SINGLE `play?device_id=…` call — a separate paused
    transfer first collapses the session on Lithe LWF1 speakers.
  * tracks/episodes play as a `uris` list; albums/playlists/artists/shows as a
    `context_uri`.
  * share links (incl. localized /intl-xx/ and ?si=… params) normalise to URIs.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.spotify.com/v1"
DEFAULT_TIMEOUT = 10


class SpotifyAPIError(Exception):
    pass


def normalize_uri(value):
    """Accept a spotify: URI or an open.spotify.com share link; return a URI."""
    value = value.strip()
    if value.startswith("spotify:"):
        return value
    m = re.match(
        r"(?:https?://)?open\.spotify\.com/(?:intl-[a-z]{2}/)?"
        r"(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)",
        value,
    )
    if m:
        return "spotify:{}:{}".format(m.group(1), m.group(2))
    return value


def _request(token, method, path, params=None, body=None, timeout=DEFAULT_TIMEOUT):
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            if not raw.strip():
                return {}  # 204 No Content (most player commands)
            try:
                return json.loads(raw)
            except ValueError:
                return {"_raw": raw}
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:300]
        if e.code == 401:
            raise SpotifyAPIError("unauthorized (401) — token expired/invalid: " + msg)
        if e.code == 403:
            raise SpotifyAPIError("forbidden (403) — needs Spotify Premium: " + msg)
        if e.code == 404:
            raise SpotifyAPIError(
                "no active device / not found (404) — open the device once or pass a "
                "device id: " + msg
            )
        if e.code == 429:
            raise SpotifyAPIError("rate limited (429): " + msg)
        raise SpotifyAPIError("Spotify API {} {} -> {} {}".format(method, path, e.code, msg))
    except (urllib.error.URLError, OSError) as e:
        raise SpotifyAPIError("Spotify API {} {} failed: {}".format(method, path, e))


# -- queries ----------------------------------------------------------------

def devices(token):
    """List the account's available Connect devices."""
    return _request(token, "GET", "/me/player/devices").get("devices", [])


def current_playback(token):
    """Current playback state of the active device ({} if nothing active)."""
    return _request(token, "GET", "/me/player") or {}


def playlists(token, cap=100):
    """The account's playlists as [{name, uri, id}], paged up to `cap`."""
    out = []
    offset = 0
    while len(out) < cap:
        page = _request(
            token, "GET", "/me/playlists", params={"limit": 50, "offset": offset}
        )
        items = page.get("items", []) if isinstance(page, dict) else []
        for p in items:
            if p and p.get("uri"):
                out.append({"name": p.get("name"), "uri": p["uri"], "id": p.get("id")})
        if not page.get("next"):
            break
        offset += 50
    return out


def find_device_id(token, name):
    """Return the id of the Connect device whose name contains `name` (ci)."""
    devs = devices(token)
    for d in devs:
        if name and name.lower() in (d.get("name") or "").lower():
            return d["id"]
    raise SpotifyAPIError(
        "no Connect device matching {!r}. Available: {}".format(
            name, [d.get("name") for d in devs] or "(none online)"
        )
    )


# -- controls ---------------------------------------------------------------

def play(token, device_id=None, context_uri=None, uris=None):
    """Start/resume playback. Pass context_uri (album/playlist/...) OR uris
    (tracks/episodes). Single call — device_id transfers automatically."""
    params = {"device_id": device_id} if device_id else None
    body = None
    if uris:
        body = {"uris": uris}
    elif context_uri:
        kind = context_uri.split(":")[1] if context_uri.count(":") >= 2 else ""
        body = {"uris": [context_uri]} if kind in ("track", "episode") else {"context_uri": context_uri}
    return _request(token, "PUT", "/me/player/play", params=params, body=body)


def pause(token, device_id=None):
    return _request(token, "PUT", "/me/player/pause", params={"device_id": device_id} if device_id else None)


def next_track(token, device_id=None):
    return _request(token, "POST", "/me/player/next", params={"device_id": device_id} if device_id else None)


def previous_track(token, device_id=None):
    return _request(token, "POST", "/me/player/previous", params={"device_id": device_id} if device_id else None)


def set_volume(token, percent, device_id=None):
    params = {"volume_percent": max(0, min(100, int(percent)))}
    if device_id:
        params["device_id"] = device_id
    return _request(token, "PUT", "/me/player/volume", params=params)
