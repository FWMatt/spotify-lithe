"""Constants for the Spotify Lithe integration."""

DOMAIN = "spotify_lithe"

OAUTH2_AUTHORIZE = "https://accounts.spotify.com/authorize"
OAUTH2_TOKEN = "https://accounts.spotify.com/api/token"

# Playback control + reading state + reading the account's playlists.
SCOPES = [
    "user-read-playback-state",
    "user-modify-playback-state",
    "playlist-read-private",
    "playlist-read-collaborative",
]

# Status poll interval (seconds). Kept modest to respect Spotify rate limits.
UPDATE_INTERVAL = 10
