# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`lithe.py` is a zero-dependency (stdlib only, Python 3.8+) CLI for controlling Lithe Audio WiFi V2 ceiling speakers over their **local** protocols (UPnP, plus a Spotify Connect controller and an internet-radio relay). No build system, package, or test suite — verify by running against a real speaker.

The repo also contains a **Spotify Connect** stack (separate from the local UPnP control, since Connect drives the speaker's own cloud client and avoids the flaky renderer):
- `spotify_api.py` — shared, stdlib, **sync** Spotify Web API client (no auth; takes an access token). Holds the hard-won logic: single-call `play?device_id=…` (a paired transfer collapses these speakers), track/episode→`uris` vs context→`context_uri`, and share-URL/`intl-`/`?si=` normalisation.
- `spotify-lithe` — standalone CLI: own PKCE OAuth + per-account token cache (`~/.config/lithe/spotify[-<account>].json`), calls `spotify_api`.
- `custom_components/spotify_lithe/` — Home Assistant integration (HAOS): OAuth via Application Credentials (one config entry per Spotify account = the multi-account seam), a `DataUpdateCoordinator` polling playback/devices/playlists, and a `media_player` entity per Connect device (playlists = `source_list`). Vendors a copy of `spotify_api.py` and runs its sync calls via `async_add_executor_job`. Can't be tested off-HAOS — `py_compile` + install on the appliance.

`lithe.py` still has its own older `SpotifyConnect` class (used by `lithe.py spotify`); the new `spotify_api.py` is the clean shared engine for the CLI + integration. They duplicate Spotify logic for now — converge if touching both.

## Running

```bash
./lithe.py discover                              # SSDP/UPnP + mDNS scan of the LAN
./lithe.py --host 192.168.1.84 probe             # fingerprint one host across protocols
./lithe.py --host 192.168.1.84 status            # or set LITHE_HOST to skip --host
LITHE_HOST=192.168.1.84 ./lithe.py vol 35
./lithe.py --host 192.168.1.84 play http://stream.example/track.mp3
./lithe.py --host 192.168.1.84 bench -n 20       # control round-trip latency stats
```

`--host` is required for every command except `discover`; it falls back to the `LITHE_HOST` env var. There is no lint/test command — verify changes by running the CLI against a real speaker (the live fleet is on the `192.168.1.x` LAN; `192.168.1.84` is "Kitchen Ceiling Speakers"). `--port` overrides the auto-discovered renderer control port.

## Architecture

The design rationale is latency: drive each speaker's control protocol directly so the speaker fetches a stream URL itself, bypassing the ~2s AirPlay 2 start buffer and reducing control to a single LAN request (tens of ms — `bench` reports ~5-10ms median).

**Critical platform note (this hardware was misidentified once — do not repeat it):** the WiFi V2 is **not** Linkplay. `httpapi.asp` returns 404. Its Google Cast receiver (`:8009`) responds to volume/status but refuses to launch a media app, so Cast cannot play a URL. The one local control surface that does everything is a standard **UPnP MediaRenderer on port 38400** (model reports as `LitheAudio LWF1`). All transport and volume control goes through its `AVTransport` and `RenderingControl` services via SOAP. This was verified empirically against live units; if you doubt it, run `probe` rather than trusting any product-name assumption.

Layers:

- **`LitheSpeaker`** — one speaker over UPnP. `_resolve()` fetches `http://<host>:<port>/description.xml`, parses the service `controlURL`s, and caches them (trying `_RENDERER_PORTS` in order: 38400 first). `_soap(service, action, args)` is the single transport chokepoint — every public method (`play`, `stop`, `volume`, `status`, …) builds its SOAP args and calls it via `_av()`/`_rc()` helpers. `play()` is `SetAVTransportURI` then `Play`, then **verifies** the transport reaches `PLAYING` within `PLAY_VERIFY_TIMEOUT`; if it stalls in `TRANSITIONING` (what continuous live/radio streams do — and that stall can crash the renderer, requiring a power-cycle) it sends `Stop` and raises. So this path is for finite files/URLs only; live streams belong to Spotify or AirPlay.
- **Discovery / fingerprinting** — `discover()` seeds an IP list from SSDP (`_ssdp_search` + `_fetch_upnp_description`) and a hand-rolled stdlib mDNS query (`discover_mdns`, parsing AirPlay/Cast/Spotify announcements), then confirms a controllable renderer on each IP via `renderer_info()`. `probe(host)` fingerprints a single host (UPnP renderer, raw HTTP `Server` header, open ports, mDNS) — the diagnostic to reach for first when a unit misbehaves.
- **Internet-radio relay** (`run_radio` / `_relay_handler` / `_RelayServer`) — the renderer stalls on continuous live streams, so `radio` runs a local HTTP relay that pulls the upstream (no `Icy-MetaData` header → plain audio) and re-serves it with a fake ~2GB `Content-Length`, which makes the renderer treat it as a finite file and play it via the normal `play` path. `_local_ip_for()` auto-detects the IP the speaker fetches from. `_RelayServer` sets `allow_reuse_address`/`daemon_threads` (else a quick stop/restart hits `TIME_WAIT` EADDRINUSE). Foreground; Ctrl-C stops the speaker and shuts the relay.
- **`SpotifyConnect`** — a *cloud* controller, not local protocol: it drives the speaker's own Spotify client via the Web API. OAuth PKCE login (stdlib — browser + one-shot localhost `:8888/callback`), tokens cached at `~/.config/lithe/spotify.json`, all calls through `_api()` (auto-refresh on 401). The `spotify` subcommands need Spotify Premium and a registered app for the client id; this path works even when the UPnP renderer is down (it doesn't touch `:38400`).
- **CLI** (`main`) — argparse subcommands map 1:1 to `LitheSpeaker` methods; `_print_timed()` wraps mutating commands to print round-trip latency. Shared Spotify options (`--client-id`, `--device`) live on a `parents=[sp_common]` parser so they're accepted *after* each `spotify` subcommand.

Things worth knowing before editing:

1. **SSDP misses the renderer intermittently.** The MediaRenderer advertises only periodically, so a single SSDP sweep often doesn't surface it (and `discover` only confirms renderers on IPs already seeded by SSDP/mDNS — a device that answers neither in the window won't appear, though direct `--host` control still works). The per-host `renderer_info()` probe in `discover` is what actually catches the units.
2. **`_soap()` is the single chokepoint** for timeouts, SOAP-fault handling (`LitheError` via `_raise_soap_fault`), and namespace-agnostic response parsing (`_parse_soap_response`). Add new device commands as methods that call it; don't open connections elsewhere. XML arg values must go through `_xml_escape` (stream URLs contain `&`).
3. **`_SSL_CTX`** disables cert verification (`CERT_NONE`) and is only used for opportunistic `https` *probing* in `_http_fingerprint`/`_fetch_upnp_description`; UPnP control itself is plain HTTP. This is intentional for a credential-free LAN path, not a bug to "fix".
4. **Spotify is a single-stream, device-switching model.** One account = one active stream, and `GET /me/player` only reports the active device — so `status --device X` can only show now-playing when X is active; otherwise it reports X's own state and names the active device. `--device` (default `None` = the active device) is honoured by `play`/`vol`/`status`; transport controls act on the active stream. `play()` of a URI must be a **single** `play?device_id=…` call — a separate paused transfer first collapses the session on these LWF1 speakers (reports "playing" then silence).
