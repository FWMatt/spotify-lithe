# lithe

A low-latency, zero-dependency command-line driver for **Lithe Audio WiFi V2** ceiling speakers.

`lithe.py` is a single Python file (standard library only, Python 3.8+). No build step, no packages to install — just run it.

```bash
./lithe.py discover
./lithe.py --host 192.168.1.84 play http://192.168.1.50:8000/song.mp3
./lithe.py --host 192.168.1.84 vol 35
./lithe.py --host 192.168.1.84 stop
```

---

## How it actually works (read this first)

The WiFi V2 (model `LitheAudio LWF1`) is **not** a Linkplay device, and its Google Cast support can't play media. The one local control surface that works for everything is a standard **UPnP MediaRenderer on port 38400**, which `lithe.py` drives directly so the speaker fetches and plays a stream URL itself — bypassing the ~2 s AirPlay buffer and giving single-request control latency (a few ms).

What this means in practice:

| You want to play… | Use |
|---|---|
| A **local file** or a **direct audio URL** (`.mp3`/`.wav`/`.flac`) | `lithe.py play` (UPnP) — **best latency** |
| **Live internet radio** (ICY/Shoutcast streams) | `lithe.py radio` (UPnP via a local relay) |
| **Spotify** | `lithe.py spotify` (Spotify Connect) — native, low latency |
| **System audio / any app** (e.g. casting an app's output) | **AirPlay 2** from your Mac/phone to the speaker (manual, GUI) |

> ⚠️ `lithe.py play` is for **finite** files/URLs — it stalls and can crash the renderer on a continuous live stream, so it now detects that and fails fast. For live radio use **`lithe.py radio`** (below), which relays the stream so the renderer accepts it.

---

## Finding your speakers

```bash
./lithe.py discover
```

Lists every media device on the LAN and marks the controllable ones, e.g.:

```
192.168.1.84      Kitchen Ceiling Speakers [UPnP :38400]
                  platforms: AirPlay 2, AirPlay audio (RAOP), Chromecast, UPnP MediaRenderer
                  device:    LitheAudio LWF1
```

To fingerprint one device across every protocol (handy when something misbehaves):

```bash
./lithe.py --host 192.168.1.84 probe
```

Set the host once to avoid repeating `--host`:

```bash
export LITHE_HOST=192.168.1.84      # lasts for this terminal session
./lithe.py status
```

---

## Playing a local MP3 (lowest latency)

The speaker plays a URL by fetching it itself, so to play a local file you serve it over HTTP from your computer and point the speaker at it. Fetching over the LAN (rather than the internet) is the fastest option — ~1 s to start.

**1. Find your computer's IP address on the speaker's network.**

```bash
ipconfig getifaddr en0        # macOS Wi-Fi; try en1 if blank
# or list all:  ifconfig | grep "inet " | grep -v 127.0.0.1
```

Use the address on the **same `192.168.x` network as the speaker** (e.g. `192.168.1.50`).

**2. Serve the folder containing your audio** (leave this terminal running):

```bash
cd ~/Music                    # the folder with your .mp3 files
python3 -m http.server 8000
```

**3. In a second terminal, tell the speaker to play it:**

```bash
cd /Users/matt/Development/LitheAudio
./lithe.py --host 192.168.1.84 play "http://192.168.1.50:8000/song.mp3"
```

Replace `192.168.1.50` with your IP from step 1, and `song.mp3` with your filename (URL-encode spaces as `%20`).

**4. When finished**, `Ctrl-C` the server terminal.

> The project ships a `test.mp3` you can use to try this out.
>
> **If `play` times out:** the speaker couldn't reach your computer. This happens when your machine and the speaker are on different subnets (routed through a gateway) rather than the same LAN — the speaker often can't connect back across that boundary. Put both on the same Wi-Fi network.

---

## Playing a direct URL

Any direct, finite audio file works without a local server:

```bash
./lithe.py --host 192.168.1.84 play "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
```

(Continuous live-radio URLs do **not** work with `play` — use `radio` below.)

---

## Internet radio

The renderer can't play a continuous live stream directly (no length, ICY metadata → it stalls). `radio` works around this with a small **local relay**: it pulls the stream, strips the ICY metadata, and re-serves it to the speaker as a length-bearing file, which the renderer happily plays.

```bash
./lithe.py --host 192.168.1.84 radio http://ice1.somafm.com/groovesalad-128-mp3
# streaming via http://<your-ip>:8088/  —  press Ctrl-C to stop
```

The command runs in the foreground and streams until you press **Ctrl-C** (which stops the speaker and shuts the relay down cleanly).

- **MP3 streams are the reliable format.** AAC/HLS (`.m3u8`) streams may not play.
- The speaker fetches from your computer, so the two must be able to reach each other (same as the local-MP3 case — easiest on the same Wi-Fi network). The relay IP is auto-detected; override with `--proxy-host`, and the port (default 8088) with `--proxy-port`.

---

## Control commands

All take `--host` (or `LITHE_HOST`):

```bash
./lithe.py --host 192.168.1.84 status        # playback state, position, volume, current track
./lithe.py --host 192.168.1.84 info          # device metadata
./lithe.py --host 192.168.1.84 vol 40        # volume 0–100
./lithe.py --host 192.168.1.84 mute          # / unmute
./lithe.py --host 192.168.1.84 pause         # / resume / toggle
./lithe.py --host 192.168.1.84 next          # / prev
./lithe.py --host 192.168.1.84 seek 90       # jump to 1:30
./lithe.py --host 192.168.1.84 stop
./lithe.py --host 192.168.1.84 bench -n 20   # measure control round-trip latency
```

Override the auto-discovered renderer port with `--port` if needed.

---

## Spotify (Spotify Connect)

The speaker runs its own Spotify client; `lithe.py` controls it via the Spotify Web API. This gives **native Spotify quality, low latency, and proper continuous streaming**. It does not stream audio through your computer.

**Requirements:**
- A **Spotify Premium** account (Web API playback control is Premium-only).
- A free registered Spotify app (for a client ID).

### One-time setup

**1. Create a Spotify app:**
- Go to <https://developer.spotify.com/dashboard> → **Create app**.
- Name it anything (e.g. "lithe").
- Under **Redirect URIs**, add exactly: `http://127.0.0.1:8888/callback`
- Save, then copy the **Client ID**.

**2. Log in (opens a browser once):**

```bash
cd /Users/matt/Development/LitheAudio
export LITHE_SPOTIFY_CLIENT_ID=<your-client-id>
./lithe.py spotify login
```

Authorize in the browser. Tokens are cached at `~/.config/lithe/spotify.json`, so this is a one-time step.

### Using it

```bash
./lithe.py spotify devices                          # list Connect devices (speakers appear when awake)
./lithe.py spotify play --device "Kitchen Ceiling Speakers"          # start on a speaker
./lithe.py spotify play spotify:playlist:37i9dQZF1DXcBWIGoYBM5M --device "Kitchen Ceiling Speakers"
./lithe.py spotify vol 40 --device "Kitchen Ceiling Speakers"
./lithe.py spotify status --device "Kitchen Ceiling Speakers"
./lithe.py spotify pause                            # / resume / next / prev (act on the active stream)
```

`play` accepts a `spotify:` URI **or** a pasted share link (`https://open.spotify.com/playlist/…?si=…`, including localized `/intl-xx/` links). **Always quote a pasted URL** — the `?si=` part makes zsh fail with `no matches found` otherwise:

```bash
./lithe.py spotify play "https://open.spotify.com/playlist/0ufL1GDmlVNj4kVHxziBfk?si=c02840ac26194339" --device "Guest WC Speaker"
```

#### Targeting a device

`--device` (substring match) is honoured by **`play`**, **`vol`**, and **`status`**. Put it after the subcommand, or set `LITHE_SPOTIFY_DEVICE` once:

```bash
export LITHE_SPOTIFY_DEVICE="Guest WC Speaker"
./lithe.py spotify play spotify:playlist:37i9dQZF1DXcBWIGoYBM5M
```

If you don't specify a device, commands act on the **currently-active** Spotify device. `pause`/`resume`/`next`/`prev` always act on the active stream (Spotify applies them there regardless of `--device`).

#### One stream at a time

A single Spotify account can only have **one active stream** — starting playback on a second speaker stops it on the first. So:

- `status --device X` shows the now-playing track only if X *is* the active device; otherwise it reports X's own state (volume, online) and names whichever device currently holds the stream.
- **Same music in multiple rooms (synced):** group the speakers in **Google Home** (they're Chromecast built-in) or via **AirPlay 2** — Spotify then sees the group as one Connect device.
- **Different music in different rooms at the same time:** not possible from one account — that needs separate accounts (Premium Duo/Family).

> Spotify Connect works even when the UPnP renderer is down — it doesn't use port 38400.

---

## Room-by-room Spotify (CLI + Home Assistant)

Two companion pieces drive Spotify Connect with a shared engine (`spotify_api.py`), independent of the flaky UPnP path:

- **`spotify-lithe`** — a standalone CLI (own OAuth, per-account token cache): `login`, `devices`, `playlists`, `play <uri|share-url>`, `pause/resume/next/prev`, `vol`, `status`, with `--device`/`--account`.
- **`custom_components/spotify_lithe/`** — a Home Assistant integration that presents each Connect speaker as a `media_player` entity (playlist dropdown + transport + volume via the built-in Media Control card). Install/usage in [its README](custom_components/spotify_lithe/README.md).

Both are Spotify Premium + a free Spotify app (client id). One stream per account; the integration's per-account config entries are the seam for the multi-account / per-room-simultaneous setup.

**Install the HA integration via HACS** (custom repository → one-click updates after):

1. In HA: **HACS → ⋮ → Custom repositories** → add `https://github.com/FWMatt/spotify-lithe`, category **Integration**.
2. **Download** "Spotify Lithe" → **restart HA** → **Settings → Devices & Services → Application Credentials** (add Spotify Client ID + Secret) → **Add Integration → Spotify Lithe** → authorize.
3. Add a **Media Control card** per room. To update later: bump `"version"` in `manifest.json`, push, then click **Update** in HACS. Full details in the [integration README](custom_components/spotify_lithe/README.md).

---

## Troubleshooting

**`error: ... no UPnP MediaRenderer on <host>` / control commands stop working.**
The renderer service (port 38400) has crashed — usually after attempting a live radio stream. The rest of the speaker (AirPlay, config UI) stays up, but the renderer does **not** reliably self-restart. **Power-cycle the speaker** (unplug ~10 s) to bring it back. Spotify and AirPlay are unaffected meanwhile.

**`play` reports "transport stuck in 'TRANSITIONING'".**
You pointed `play` at a continuous live stream, which the renderer can't play directly. The command stops the device cleanly and aborts. Use a direct `.mp3`/`.wav`/`.flac` URL with `play`, or use **`radio`** (which relays the stream) for live internet radio.

**`spotify` returns 403.**
Web API playback control requires Spotify Premium.

**`spotify` returns 404 / "no active device".**
Wake the speaker once in the Spotify app (or pass a track URI to `play`) so Spotify has a device to target.

**`discover` doesn't list a speaker you know is there.**
The renderer advertises only periodically, so a single scan can miss it. Re-run, or just use `--host <ip>` directly — control works regardless.

---

## Command reference

```
discover                 Find media devices on the LAN (SSDP/UPnP + mDNS)
probe                    Fingerprint --host across protocols
info                     Renderer metadata
status                   Playback state
play <url>               Play a direct audio URL / local-served file
radio <url>              Stream a live internet-radio URL via a local relay (Ctrl-C to stop)
stop | pause | resume | toggle | next | prev
seek <seconds>           Seek to a position
vol <0-100>              Set volume
mute | unmute
bench [-n N]             Measure control round-trip latency

spotify login            Authorize (one-time, opens browser)
spotify devices          List Connect devices
spotify play [uri]       Play on the target device (optional track/playlist URI/link)
spotify pause | resume | next | prev
spotify vol <0-100>
spotify status

Global:  --host <ip>  (or LITHE_HOST)   --port <n>   --timeout <s>
Spotify: --client-id <id>  (or LITHE_SPOTIFY_CLIENT_ID)   --device "<name>"  (or LITHE_SPOTIFY_DEVICE)
```
