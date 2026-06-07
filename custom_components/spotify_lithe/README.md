# Spotify Lithe — Home Assistant integration

Room-by-room **Spotify Connect** control for the Lithe Audio speakers (or any
Spotify Connect device), as native Home Assistant `media_player` entities. Each
speaker becomes an entity with play/pause, next/prev, volume, and a **playlist
dropdown** (the account's playlists) — so the built-in **Media Control card**
gives you the whole room player with no custom frontend.

It controls Spotify Connect directly (the speaker streams from Spotify itself),
so it **does not** use the speakers' flaky UPnP/DLNA path — far more reliable
than streaming via Music Assistant/DLNA.

## Why this exists

The Lithe WiFi V2 (LWF1) UPnP renderer buffers slowly and crashes when fed a
stream (that's how Music Assistant drives it). Spotify Connect sidesteps that
entirely. See the repo's `CLAUDE.md` for the full hardware story.

## Prerequisites

- **Spotify Premium** (Web API playback control is Premium-only).
- A free **Spotify app** for the client id/secret:
  - <https://developer.spotify.com/dashboard> → **Create app**.
  - Add redirect URI **exactly**: `https://my.home-assistant.io/redirect/oauth`
  - Copy the **Client ID** and **Client Secret**.

## Install — via HACS (recommended, easy to update)

The repo is a HACS **custom repository**: add it once, then updates are a
one-click button in HACS. Requires [HACS](https://hacs.xyz/docs/use/download/download/)
installed on your HA.

1. Push this repo to GitHub (from the project root):
   ```bash
   git init && git add . && git commit -m "Spotify Lithe"
   gh repo create FWMatt/spotify-lithe --public --source=. --push
   ```
   (Set the `documentation`/`issue_tracker` URLs in `manifest.json` to your repo.)
2. In HA: **HACS → ⋮ → Custom repositories** → add
   `https://github.com/FWMatt/spotify-lithe`, category **Integration**.
3. Search **Spotify Lithe** → **Download** → **Restart Home Assistant**.
4. **Settings → Devices & Services → Application Credentials** → add the Spotify
   Client ID and Secret.
5. **Add Integration → Spotify Lithe** → authorize in the browser.
6. One `media_player.<speaker>` entity appears per Spotify Connect device the
   account can currently see (asleep speakers appear once woken; they then
   persist and show *unavailable* when offline).

### Updating
Edit code → bump `"version"` in `manifest.json` → commit & push (optionally
`gh release create vX.Y.Z`). HACS shows an update button → click it → restart.
The bundled `.github/workflows/validate.yml` runs HACS + hassfest checks on
every push.

## Install — manual (alternative)

Copy this folder to `/config/custom_components/spotify_lithe/`, restart HA, then
do steps 4–6 above. Simple, but you update by re-copying the files.

## The card

Add a **Media Control card** and point it at a room's entity. You get the
playlist (source) dropdown, now-playing, play/pause, next/prev and volume. One
card per room. (`mini-media-player` is an optional nicety for denser styling.)

## Multi-account (per-room simultaneous streams)

Spotify allows **one active stream per account**, so with a single account only
one room plays Spotify at a time. To run different music in different rooms at
once, add a **second config entry** (a different Spotify Family account) — each
entry is independent. First link each speaker to its own account in the Lithe /
Spotify app (the device-side step the integration can't do).

## Verification checklist (run on HAOS after install)

1. Integration loads with no errors in the log; OAuth completes.
2. `media_player` entities appear — one per awake Connect speaker.
3. The card's source dropdown lists your Spotify playlists.
4. Selecting a playlist starts it on that speaker (and that entity becomes the
   active one showing the track).
5. Play/pause, next/prev and the volume slider all work.
6. Now-playing (title/artist) shows on the active room.

> Known, inherent limit: a room that isn't the active Spotify stream shows
> *idle* with no track until you select a playlist on it — Spotify only reports
> now-playing for the single active device. Multi-account removes this per-room.

## CLI companion

`spotify-lithe` (repo root) is the standalone CLI built on the same engine
(`spotify_api.py`): `login`, `devices`, `playlists`, `play`, `pause/resume/
next/prev`, `vol`, `status`, with `--device` and `--account`. Handy for
scripting and for confirming behaviour outside HA.
