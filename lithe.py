#!/usr/bin/env python3
"""
lithe - a low-latency command-line driver for Lithe Audio WiFi V2 ceiling speakers.

The WiFi V2 (model "LitheAudio LWF1") exposes a standard UPnP MediaRenderer on
a dedicated control port (38400 on current firmware). Driving its AVTransport
and RenderingControl services directly lets the speaker pull a stream URL
itself and gives single-SOAP-POST control latency (tens of ms on LAN), without
the ~2s AirPlay 2 start buffer.

It does NOT speak Linkplay's httpapi.asp (that returns 404), and its Google Cast
receiver does not allow launching a media app, so neither is used here. UPnP is
the one local control surface that does everything, and it needs no third-party
libraries.

Zero third-party dependencies. Python 3.8+. Standard library only.

Quick start
-----------
  ./lithe.py discover                          find media devices on this network
  ./lithe.py --host 192.168.1.84 probe         fingerprint one host across protocols
  ./lithe.py --host 192.168.1.84 info          renderer metadata
  ./lithe.py --host 192.168.1.84 status        playback state
  ./lithe.py --host 192.168.1.84 play http://stream.example/track.mp3
  ./lithe.py --host 192.168.1.84 vol 35
  ./lithe.py --host 192.168.1.84 stop
  ./lithe.py --host 192.168.1.84 bench -n 20    measure control round-trip latency

The host can also be set once via the LITHE_HOST environment variable. The
renderer control port is auto-discovered; override it with --port if needed.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import socketserver
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET

DEFAULT_TIMEOUT = 3.0  # seconds. Speakers on a healthy LAN answer well under this.
PLAY_TIMEOUT = 12.0    # SetAVTransportURI may block while the device prefetches.
PLAY_START_TIMEOUT = 6.0  # how long the Play call itself may block before we verify.
PLAY_VERIFY_TIMEOUT = 3.0  # after Play, how long to wait for the PLAYING state.

# Control ports the LWF1 renderer has been seen on, tried in order during
# auto-discovery. 38400 is current firmware; the others are common Linkplay-era
# fallbacks.
_RENDERER_PORTS = [38400, 49152, 59152, 8080]

_AVT = "urn:schemas-upnp-org:service:AVTransport:1"
_RC = "urn:schemas-upnp-org:service:RenderingControl:1"

# A single SSL context that does not verify, reused for any https probing. Some
# Linkplay-era endpoints present a self-signed cert; UPnP control itself is
# plain http. We never send credentials, so this is fine for a LAN control path.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


class LitheError(Exception):
    pass


def _xml_escape(value):
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _local(tag):
    """Strip the XML namespace from an element tag."""
    return tag.rsplit("}", 1)[-1]


def _desc_field(root, name):
    for el in root.iter():
        if _local(el.tag) == name and el.text:
            return el.text.strip()
    return None


def _parse_soap_response(text, action):
    """Return the out-args of a SOAP response as {tag: text}."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise LitheError("malformed SOAP response: {!r}".format(text[:200]))
    for el in root.iter():
        if _local(el.tag) == action + "Response":
            return {_local(c.tag): c.text for c in el}
    return {}


def _raise_soap_fault(text, action):
    """Turn a SOAP 500 body into a LitheError (UPnPError if present)."""
    try:
        root = ET.fromstring(text)
        code = _desc_field(root, "errorCode")
        desc = _desc_field(root, "errorDescription")
        if code or desc:
            raise LitheError("UPnP error on {}: {} ({})".format(action, desc, code))
    except ET.ParseError:
        pass
    raise LitheError("SOAP fault on {}: {!r}".format(action, text[:200]))


class LitheSpeaker:
    """One Lithe WiFi V2, controlled over its UPnP MediaRenderer."""

    def __init__(self, host, timeout=DEFAULT_TIMEOUT, port=None):
        self.host = host
        self.timeout = timeout
        self.port = port  # explicit control port, or None to auto-discover
        self._services = None      # {"AVTransport": (type, url), "RenderingControl": ...}
        self._control_port = None
        self._desc = {}

    # -- renderer resolution ----------------------------------------------

    def _resolve(self):
        """Find the renderer's description and service control URLs (cached)."""
        if self._services is not None:
            return
        ports = [self.port] if self.port else _RENDERER_PORTS
        last_err = None
        for port in ports:
            base = "http://{}:{}".format(self.host, port)
            try:
                with urllib.request.urlopen(
                    base + "/description.xml", timeout=self.timeout
                ) as resp:
                    xml = resp.read()
                root = ET.fromstring(xml)
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
                last_err = e
                continue
            except ET.ParseError as e:
                last_err = e
                continue

            services = {}
            for svc in root.iter():
                if _local(svc.tag) != "service":
                    continue
                d = {_local(c.tag): (c.text or "").strip() for c in svc}
                stype = d.get("serviceType", "")
                control = urllib.parse.urljoin(base + "/", d.get("controlURL", ""))
                if "AVTransport" in stype:
                    services["AVTransport"] = (stype, control)
                elif "RenderingControl" in stype:
                    services["RenderingControl"] = (stype, control)

            if "AVTransport" in services:
                self._services = services
                self._control_port = port
                self._desc = {
                    "friendlyName": _desc_field(root, "friendlyName"),
                    "manufacturer": _desc_field(root, "manufacturer"),
                    "modelName": _desc_field(root, "modelName"),
                    "UDN": _desc_field(root, "UDN"),
                    "control_port": port,
                }
                return
        raise LitheError(
            "no UPnP MediaRenderer on {} (tried ports {}): {}".format(
                self.host, ",".join(str(p) for p in ports), last_err
            )
        )

    def renderer_info(self):
        self._resolve()
        info = dict(self._desc)
        info["services"] = sorted(self._services.keys())
        return info

    # -- SOAP transport ----------------------------------------------------

    def _soap(self, service, action, args=(), timeout=None):
        """Send one SOAP action. Returns the response out-args as a dict."""
        self._resolve()
        if service not in self._services:
            raise LitheError("device has no {} service".format(service))
        stype, control = self._services[service]
        timeout = self.timeout if timeout is None else timeout
        inner = "".join(
            "<{0}>{1}</{0}>".format(k, _xml_escape(str(v))) for k, v in args
        )
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body><u:{a} xmlns:u=\"{t}\">{i}</u:{a}></s:Body></s:Envelope>"
        ).format(a=action, t=stype, i=inner).encode("utf-8")
        req = urllib.request.Request(
            control,
            data=body,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": '"{}#{}"'.format(stype, action),
                "Connection": "close",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            _raise_soap_fault(e.read().decode("utf-8", "replace"), action)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            raise LitheError("SOAP {} failed on {}: {}".format(action, self.host, e))
        return _parse_soap_response(text, action)

    def _av(self, action, args=(), timeout=None):
        return self._soap("AVTransport", action, args, timeout)

    def _rc(self, action, args=()):
        return self._soap("RenderingControl", action, args)

    # -- queries -----------------------------------------------------------

    def info(self):
        """Renderer metadata (name, model, manufacturer, UDN, control port)."""
        return self.renderer_info()

    def status(self):
        """Playback state: transport state, position, volume, current track."""
        ti = self._av("GetTransportInfo", [("InstanceID", 0)])
        pi = self._av("GetPositionInfo", [("InstanceID", 0)])
        vol = self._rc("GetVolume", [("InstanceID", 0), ("Channel", "Master")])
        mute = self._rc("GetMute", [("InstanceID", 0), ("Channel", "Master")])

        def _int(d, key):
            try:
                return int(d.get(key))
            except (TypeError, ValueError):
                return d.get(key)

        return {
            "state": ti.get("CurrentTransportState"),
            "position": pi.get("RelTime"),
            "duration": pi.get("TrackDuration"),
            "track_uri": pi.get("TrackURI"),
            "volume": _int(vol, "CurrentVolume"),
            "muted": bool(_int(mute, "CurrentMute")),
        }

    # -- transport controls ------------------------------------------------

    def _await_state(self, target, timeout):
        """Poll GetTransportInfo until `target`, a terminal state, or timeout."""
        deadline = time.time() + timeout
        state = None
        while True:
            state = self._av("GetTransportInfo", [("InstanceID", 0)]).get(
                "CurrentTransportState"
            )
            if state == target or state in ("STOPPED", "NO_MEDIA_PRESENT"):
                return state
            if time.time() >= deadline:
                return state
            time.sleep(0.4)

    def play(self, url, metadata=""):
        # Hand the speaker the URL, start it, then VERIFY it actually started.
        # This renderer plays finite audio files but stalls forever in
        # TRANSITIONING on continuous live/radio streams — and a stuck Play can
        # crash its UPnP service. So if it doesn't reach PLAYING quickly we send
        # Stop to release it and fail with a clear message instead of hanging.
        self._av(
            "SetAVTransportURI",
            [("InstanceID", 0), ("CurrentURI", url), ("CurrentURIMetaData", metadata)],
            timeout=PLAY_TIMEOUT,
        )
        try:
            self._av("Play", [("InstanceID", 0), ("Speed", 1)], timeout=PLAY_START_TIMEOUT)
        except LitheError:
            # The device blocks the Play response until buffered; on a stream it
            # never returns. Don't treat that as fatal yet — confirm via state.
            pass
        state = self._await_state("PLAYING", PLAY_VERIFY_TIMEOUT)
        if state != "PLAYING":
            try:
                self.stop()  # release the renderer so it stops churning on the URL
            except LitheError:
                pass
            raise LitheError(
                "playback did not start (transport stuck in {!r}). This renderer "
                "plays finite audio files, not continuous live/radio streams — "
                "use a direct .mp3/.wav/.flac URL.".format(state)
            )
        return state

    def stop(self):
        return self._av("Stop", [("InstanceID", 0)])

    def pause(self):
        return self._av("Pause", [("InstanceID", 0)])

    def resume(self):
        return self._av("Play", [("InstanceID", 0), ("Speed", 1)], timeout=PLAY_TIMEOUT)

    def toggle(self):
        state = self._av("GetTransportInfo", [("InstanceID", 0)]).get(
            "CurrentTransportState"
        )
        return self.pause() if state == "PLAYING" else self.resume()

    def next(self):
        return self._av("Next", [("InstanceID", 0)])

    def prev(self):
        return self._av("Previous", [("InstanceID", 0)])

    def seek(self, seconds):
        seconds = max(0, int(seconds))
        target = "{:02d}:{:02d}:{:02d}".format(
            seconds // 3600, (seconds % 3600) // 60, seconds % 60
        )
        return self._av(
            "Seek", [("InstanceID", 0), ("Unit", "REL_TIME"), ("Target", target)]
        )

    def volume(self, level):
        level = max(0, min(100, int(level)))
        return self._rc(
            "SetVolume",
            [("InstanceID", 0), ("Channel", "Master"), ("DesiredVolume", level)],
        )

    def mute(self, on=True):
        return self._rc(
            "SetMute",
            [("InstanceID", 0), ("Channel", "Master"), ("DesiredMute", 1 if on else 0)],
        )

    # -- latency benchmark -------------------------------------------------

    def bench(self, n=20):
        """Fire n lightweight GetTransportInfo calls and report round-trip latency."""
        self._av("GetTransportInfo", [("InstanceID", 0)])  # warm resolution + connection
        samples = []
        for _ in range(n):
            t0 = time.perf_counter()
            self._av("GetTransportInfo", [("InstanceID", 0)])
            samples.append((time.perf_counter() - t0) * 1000.0)  # ms
        samples.sort()
        return {
            "samples": n,
            "min_ms": round(samples[0], 1),
            "median_ms": round(samples[len(samples) // 2], 1),
            "p95_ms": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))], 1),
            "max_ms": round(samples[-1], 1),
            "control_port": self._control_port,
        }


# -- SSDP / UPnP discovery -------------------------------------------------

def _ssdp_search(timeout=3.0, st="ssdp:all"):
    """SSDP M-SEARCH. Returns {ip: {header_lower: value}} from all responders."""
    msearch = "\r\n".join(
        [
            "M-SEARCH * HTTP/1.1",
            "HOST: 239.255.255.250:1900",
            'MAN: "ssdp:discover"',
            "MX: 2",
            "ST: " + st,
            "",
            "",
        ]
    ).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    try:
        sock.sendto(msearch, ("239.255.255.250", 1900))
    except OSError:
        sock.close()
        return {}

    responders = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(65507)
        except (socket.timeout, OSError):
            break
        headers = {}
        for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        responders.setdefault(addr[0], {}).update(headers)
    sock.close()
    return responders


def _fetch_upnp_description(location, timeout=2.0):
    """Fetch + parse a UPnP device description XML (the SSDP LOCATION URL)."""
    if not location:
        return {}
    try:
        kwargs = {"timeout": timeout}
        if location.lower().startswith("https"):
            kwargs["context"] = _SSL_CTX
        with urllib.request.urlopen(location, **kwargs) as resp:
            body = resp.read()
        root = ET.fromstring(body)
    except (urllib.error.URLError, socket.timeout, OSError, ET.ParseError):
        return {}
    return {
        "manufacturer": _desc_field(root, "manufacturer"),
        "model": _desc_field(root, "modelName") or _desc_field(root, "modelNumber"),
        "friendlyName": _desc_field(root, "friendlyName"),
    }


# -- mDNS / Bonjour --------------------------------------------------------

_MDNS_ADDR = ("224.0.0.251", 5353)
_MDNS_SERVICES = {
    "_airplay._tcp.local": "AirPlay 2",
    "_raop._tcp.local": "AirPlay audio (RAOP)",
    "_googlecast._tcp.local": "Chromecast",
    "_spotify-connect._tcp.local": "Spotify Connect",
    "_linkplay._tcp.local": "Linkplay",
}


def _dns_encode_name(name):
    out = bytearray()
    for label in name.split("."):
        if label:
            b = label.encode("utf-8")
            out.append(len(b))
            out += b
    out.append(0)
    return bytes(out)


def _dns_read_name(data, offset):
    """Read a DNS name, following 0xC0 compression pointers. -> (name, next)."""
    labels = []
    next_offset = None
    jumps = 0
    while 0 <= offset < len(data):
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:  # compression pointer
            if offset + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if next_offset is None:
                next_offset = offset + 2
            offset = pointer
            jumps += 1
            if jumps > 64:  # guard against pointer loops
                break
            continue
        offset += 1
        labels.append(data[offset:offset + length].decode("utf-8", "replace"))
        offset += length
    if next_offset is None:
        next_offset = offset
    return ".".join(labels), next_offset


def _mdns_parse(data):
    """Yield (name, rtype, parsed_rdata) for every record in an mDNS packet."""
    out = []
    if len(data) < 12:
        return out
    qd, an, ns, ar = struct.unpack(">HHHH", data[4:12])
    offset = 12
    for _ in range(qd):  # skip questions
        _, offset = _dns_read_name(data, offset)
        offset += 4
    for _ in range(an + ns + ar):
        name, offset = _dns_read_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        rdata_off = offset
        offset += rdlen
        parsed = None
        if rtype == 1 and rdlen == 4:  # A
            parsed = ".".join(str(b) for b in data[rdata_off:rdata_off + 4])
        elif rtype == 12:  # PTR
            parsed, _ = _dns_read_name(data, rdata_off)
        elif rtype == 33 and rdlen >= 6:  # SRV
            _pri, _wt, port = struct.unpack(">HHH", data[rdata_off:rdata_off + 6])
            target, _ = _dns_read_name(data, rdata_off + 6)
            parsed = (port, target)
        out.append((name, rtype, parsed))
    return out


def discover_mdns(timeout=3.0):
    """Multicast-DNS PTR query for AirPlay/Chromecast/Spotify/Linkplay services.

    Returns a list of {protocol, instance, host, ip, port}.
    """
    services = list(_MDNS_SERVICES.keys())
    header = struct.pack(">HHHHHH", 0, 0, len(services), 0, 0, 0)
    query = header + b"".join(
        _dns_encode_name(s) + struct.pack(">HH", 12, 1) for s in services
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    try:
        sock.sendto(query, _MDNS_ADDR)
    except OSError:
        sock.close()
        return []

    a_records = {}    # hostname -> ip
    srv_records = {}  # instance -> (port, target)
    ptr_records = {}  # service -> set(instances)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, _addr = sock.recvfrom(65535)
        except (socket.timeout, OSError):
            break
        for name, rtype, parsed in _mdns_parse(data):
            if rtype == 1:
                a_records[name] = parsed
            elif rtype == 12 and parsed:
                ptr_records.setdefault(name, set()).add(parsed)
            elif rtype == 33 and parsed:
                srv_records[name] = parsed
    sock.close()

    results = []
    for service, instances in ptr_records.items():
        label = next(
            (nice for stype, nice in _MDNS_SERVICES.items() if service.startswith(stype)),
            service,
        )
        for inst in instances:
            port, target = srv_records.get(inst, (None, None))
            results.append(
                {
                    "protocol": label,
                    "instance": inst,
                    "host": target,
                    "ip": a_records.get(target) if target else None,
                    "port": port,
                }
            )
    return results


# -- per-host fingerprint --------------------------------------------------

def _http_fingerprint(host, path="/", timeout=2.0):
    """Raw HTTP(S) GET to read status + Server header. Returns dict or None."""
    for scheme in ("http", "https"):
        url = "{}://{}{}".format(scheme, host, path)
        try:
            kwargs = {"timeout": timeout}
            if scheme == "https":
                kwargs["context"] = _SSL_CTX
            with urllib.request.urlopen(url, **kwargs) as resp:
                snippet = resp.read(256).decode("utf-8", "replace").strip()
                return {
                    "scheme": scheme,
                    "status": resp.status,
                    "server": resp.headers.get("Server", ""),
                    "snippet": snippet[:160],
                }
        except urllib.error.HTTPError as e:
            return {
                "scheme": scheme,
                "status": e.code,
                "server": e.headers.get("Server", ""),
                "snippet": "",
            }
        except (urllib.error.URLError, socket.timeout, OSError):
            continue
    return None


def _tcp_open(host, port, timeout=1.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


# Telltale TCP ports for the platforms a Lithe WiFi V2 actually runs.
_PROBE_PORTS = [
    (80, "HTTP (config UI)"),
    (443, "HTTPS"),
    (7000, "AirPlay (RTSP)"),
    (8008, "Chromecast (HTTP)"),
    (8009, "Chromecast (TLS)"),
    (38400, "UPnP MediaRenderer"),
]


def probe(host, timeout=2.0):
    """Fingerprint a single host across protocols to identify what it is."""
    findings = {"host": host}

    try:
        findings["upnp_renderer"] = dict(
            LitheSpeaker(host, timeout=timeout).renderer_info(), detected=True
        )
    except LitheError as e:
        findings["upnp_renderer"] = {"detected": False, "reason": str(e)}

    findings["http"] = _http_fingerprint(host, "/", timeout)
    findings["open_ports"] = [
        {"port": port, "service": label}
        for port, label in _PROBE_PORTS
        if _tcp_open(host, port, min(timeout, 1.0))
    ]
    # mDNS is network-wide; keep only announcements resolving to this host.
    findings["mdns"] = [m for m in discover_mdns(timeout=3.0) if m.get("ip") == host]
    return findings


def discover(timeout=3.0):
    """Find media devices on the LAN via SSDP+UPnP and mDNS, then identify them.

    LAN only. Returns a list of dicts: {ip, name, platforms, renderer,
    control_port, manufacturer, model}. Identification is empirical (read from
    each device), not assumed from the product name.
    """
    by_ip = {}

    def _entry(ip):
        return by_ip.setdefault(
            ip, {"ip": ip, "name": None, "platforms": set(), "renderer": False}
        )

    # SSDP: collect responders, fetch each device description for manufacturer/model.
    for ip, headers in _ssdp_search(timeout).items():
        entry = _entry(ip)
        entry["platforms"].add("UPnP/SSDP")
        desc = _fetch_upnp_description(headers.get("location"), timeout=2.0)
        if desc.get("manufacturer"):
            entry["manufacturer"] = desc["manufacturer"]
        if desc.get("model"):
            entry["model"] = desc["model"]
        if desc.get("friendlyName") and not entry["name"]:
            entry["name"] = desc["friendlyName"]

    # mDNS: AirPlay / Chromecast / Spotify / Linkplay announcements.
    for m in discover_mdns(timeout):
        ip = m.get("ip")
        if not ip:
            continue
        entry = _entry(ip)
        entry["platforms"].add(m["protocol"])
        if m.get("instance") and not entry["name"]:
            entry["name"] = m["instance"].split(".")[0]

    # Confirm a controllable UPnP MediaRenderer on each candidate (SSDP can miss
    # it, since the renderer advertises only periodically) and record its port.
    for ip, entry in by_ip.items():
        try:
            ri = LitheSpeaker(ip, timeout=1.5).renderer_info()
        except LitheError:
            continue
        entry["renderer"] = True
        entry["control_port"] = ri.get("control_port")
        entry["platforms"].add("UPnP MediaRenderer")
        entry["name"] = ri.get("friendlyName") or entry["name"]
        if ri.get("manufacturer"):
            entry["manufacturer"] = ri["manufacturer"]
        if ri.get("modelName"):
            entry["model"] = ri["modelName"]

    found = []
    for entry in by_ip.values():
        entry["platforms"] = sorted(entry["platforms"])
        entry["name"] = entry["name"] or "(unknown)"
        found.append(entry)
    found.sort(key=lambda e: tuple(int(p) for p in e["ip"].split(".") if p.isdigit()))
    return found


# -- Spotify Connect --------------------------------------------------------
#
# Spotify Connect is not something you stream *to* (like AirPlay/UPnP). The
# speaker runs its own Spotify client that pulls audio directly from Spotify;
# we just act as a controller over the Web API. That gives native Spotify
# quality, low latency, and proper continuous streaming. Requires a Spotify
# Premium account and a (free) registered Spotify app for the client id.
#
# Auth is OAuth 2.0 Authorization Code with PKCE — no client secret needed,
# which suits a CLI. Tokens are cached so 'login' is a one-time step.

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_SCOPES = "user-read-playback-state user-modify-playback-state"
SPOTIFY_REDIRECT_PORT = 8888
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:{}/callback".format(SPOTIFY_REDIRECT_PORT)
_SPOTIFY_CFG = os.path.expanduser("~/.config/lithe/spotify.json")


class SpotifyError(LitheError):
    pass


class SpotifyConnect:
    """Controls Spotify playback on a Connect device via the Web API."""

    def __init__(self, client_id=None):
        self.cfg = self._load_cfg()
        self.client_id = client_id or self.cfg.get("client_id")

    # -- config / token cache ---------------------------------------------

    def _load_cfg(self):
        try:
            with open(_SPOTIFY_CFG) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_cfg(self):
        os.makedirs(os.path.dirname(_SPOTIFY_CFG), exist_ok=True)
        with open(_SPOTIFY_CFG, "w") as f:
            json.dump(self.cfg, f, indent=2)
        try:
            os.chmod(_SPOTIFY_CFG, 0o600)  # tokens are sensitive
        except OSError:
            pass

    # -- OAuth (PKCE) ------------------------------------------------------

    def login(self):
        if not self.client_id:
            raise SpotifyError(
                "no client id. Create a free app at https://developer.spotify.com "
                "(add redirect URI {}), then set LITHE_SPOTIFY_CLIENT_ID or pass "
                "--client-id.".format(SPOTIFY_REDIRECT_URI)
            )
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        state = secrets.token_urlsafe(16)
        url = SPOTIFY_AUTH_URL + "?" + urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": SPOTIFY_REDIRECT_URI,
                "scope": SPOTIFY_SCOPES,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "state": state,
            }
        )

        result = {}

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                result["code"] = qs.get("code", [None])[0]
                result["state"] = qs.get("state", [None])[0]
                result["error"] = qs.get("error", [None])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>lithe: Spotify authorized. You can close this tab.</h2>")

            def log_message(self, *a):
                pass

        try:
            httpd = http.server.HTTPServer(("127.0.0.1", SPOTIFY_REDIRECT_PORT), _Handler)
        except OSError as e:
            raise SpotifyError("could not bind {}: {}".format(SPOTIFY_REDIRECT_URI, e))
        print("Opening browser to authorize Spotify...")
        print("If it doesn't open, visit:\n  " + url)
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            while not result:
                httpd.handle_request()
        finally:
            httpd.server_close()

        if result.get("error"):
            raise SpotifyError("authorization denied: " + result["error"])
        if result.get("state") != state:
            raise SpotifyError("state mismatch (possible CSRF); aborting")
        if not result.get("code"):
            raise SpotifyError("no authorization code received")

        tok = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": SPOTIFY_REDIRECT_URI,
                "client_id": self.client_id,
                "code_verifier": verifier,
            }
        )
        self._store_token(tok)
        print("Logged in. Tokens cached at " + _SPOTIFY_CFG)

    def _token_request(self, fields):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(
            SPOTIFY_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise SpotifyError(
                "token request failed: {} {}".format(e.code, e.read().decode()[:200])
            )

    def _store_token(self, tok):
        self.cfg["access_token"] = tok["access_token"]
        self.cfg["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600)) - 30
        if tok.get("refresh_token"):  # not always returned on refresh
            self.cfg["refresh_token"] = tok["refresh_token"]
        self.cfg["client_id"] = self.client_id
        self._save_cfg()

    def _refresh(self):
        rt = self.cfg.get("refresh_token")
        if not rt:
            raise SpotifyError("not logged in; run 'spotify login'")
        self._store_token(
            self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": rt,
                    "client_id": self.client_id,
                }
            )
        )

    def _access_token(self):
        if not self.cfg.get("access_token"):
            raise SpotifyError("not logged in; run 'spotify login'")
        if time.time() >= self.cfg.get("expires_at", 0):
            self._refresh()
        return self.cfg["access_token"]

    # -- Web API -----------------------------------------------------------

    def _api(self, method, path, params=None, body=None):
        def do(token):
            url = SPOTIFY_API + path
            if params:
                url += "?" + urllib.parse.urlencode(params)
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode()
                if not raw.strip():
                    return {}  # player commands return 204 No Content on success
                try:
                    return json.loads(raw)
                except ValueError:
                    return {"_raw": raw}  # tolerate non-JSON success bodies

        try:
            return do(self._access_token())
        except urllib.error.HTTPError as e:
            if e.code == 401:  # token went stale mid-flight; refresh once and retry
                self._refresh()
                try:
                    return do(self.cfg["access_token"])
                except urllib.error.HTTPError as e2:
                    e = e2
            msg = e.read().decode()[:300]
            if e.code == 403:
                raise SpotifyError(
                    "Spotify refused (403). Web API playback control requires Spotify "
                    "Premium. " + msg
                )
            if e.code == 404:
                raise SpotifyError(
                    "no active device / not found (404). Open the target device once "
                    "in the Spotify app, or pass a track URI to 'play'. " + msg
                )
            raise SpotifyError(
                "Spotify API {} {} -> {} {}".format(method, path, e.code, msg)
            )

    def devices(self):
        return self._api("GET", "/me/player/devices").get("devices", [])

    def _device_id(self, name):
        devs = self.devices()
        for d in devs:
            if name and name.lower() in d["name"].lower():
                return d["id"]
        raise SpotifyError(
            "no Connect device matching {!r}. Available: {}".format(
                name, [d["name"] for d in devs] or "(none online)"
            )
        )

    def play(self, device_name=None, uri=None):
        did = self._device_id(device_name) if device_name else None
        if uri:
            # Start the content on the target device in ONE call — passing
            # device_id transfers playback automatically. Doing a separate
            # paused transfer first collapses the Spotify session on these LWF1
            # speakers (reports "playing" then drops to silence).
            kind = uri.split(":")[1] if uri.startswith("spotify:") and uri.count(":") >= 2 else ""
            # tracks/episodes are played as a uri list; albums/playlists/artists/shows as a context
            body = {"uris": [uri]} if kind in ("track", "episode") else {"context_uri": uri}
            self._api("PUT", "/me/player/play", params={"device_id": did} if did else None, body=body)
        elif did:
            # No URI: transfer to the device and resume its current context.
            self._api("PUT", "/me/player", body={"device_ids": [did], "play": True})
        else:
            self._api("PUT", "/me/player/play")  # resume on the active device

    def pause(self):
        self._api("PUT", "/me/player/pause")

    def resume(self):
        self._api("PUT", "/me/player/play")

    def next(self):
        self._api("POST", "/me/player/next")

    def prev(self):
        self._api("POST", "/me/player/previous")

    def volume(self, level, device_name=None):
        params = {"volume_percent": max(0, min(100, int(level)))}
        if device_name:
            params["device_id"] = self._device_id(device_name)
        self._api("PUT", "/me/player/volume", params=params)

    def status(self):
        return self._api("GET", "/me/player") or {}


def _normalize_spotify_uri(s):
    """Accept a spotify: URI or an open.spotify.com share link; return a spotify: URI.

    Handles localized links (e.g. /intl-de/), a missing scheme, and the trailing
    ?si=... query param that share URLs carry.
    """
    s = s.strip()
    if s.startswith("spotify:"):
        return s
    m = re.match(
        r"(?:https?://)?open\.spotify\.com/(?:intl-[a-z]{2}/)?"
        r"(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)",
        s,
    )
    if m:
        return "spotify:{}:{}".format(m.group(1), m.group(2))
    return s


def _spotify_cli(args):
    sp = SpotifyConnect(client_id=args.client_id)
    try:
        if args.spcmd == "login":
            sp.login()
        elif args.spcmd == "devices":
            devs = sp.devices()
            if not devs:
                print("No Connect devices online. Wake the speaker in the Spotify app.")
                return 1
            for d in devs:
                print(
                    "{:<26} {:<12} vol={:<4} {}".format(
                        d["name"], d["type"], d.get("volume_percent"),
                        "ACTIVE" if d.get("is_active") else "",
                    ).rstrip()
                )
        elif args.spcmd == "play":
            uri = _normalize_spotify_uri(args.uri) if args.uri else None
            sp.play(args.device, uri)
            print("playing" + (" on " + args.device if args.device else ""))
        elif args.spcmd == "pause":
            sp.pause(); print("paused")
        elif args.spcmd == "resume":
            sp.resume(); print("resumed")
        elif args.spcmd == "next":
            sp.next(); print("next")
        elif args.spcmd == "prev":
            sp.prev(); print("prev")
        elif args.spcmd == "vol":
            sp.volume(args.level, args.device)
            print("vol {}".format(args.level) + (" on " + args.device if args.device else ""))
        elif args.spcmd == "status":
            if args.device:
                # Spotify tracks only ONE active stream, so an inactive device has
                # no "now playing" — report its own state and name the active device.
                devs = sp.devices()
                match = next((d for d in devs if args.device.lower() in d["name"].lower()), None)
                if match is None:
                    print("device {!r} is not online. Available: {}".format(
                        args.device, [d["name"] for d in devs] or "(none)"))
                    return 1
                if match.get("is_active"):
                    st = sp.status()
                    item = st.get("item") or {}
                    print(json.dumps({
                        "device": match["name"],
                        "is_active": True,
                        "is_playing": st.get("is_playing"),
                        "track": item.get("name"),
                        "artists": ", ".join(a["name"] for a in item.get("artists", [])),
                        "volume": match.get("volume_percent"),
                    }, indent=2))
                else:
                    active = next((d["name"] for d in devs if d.get("is_active")), None)
                    print(json.dumps({
                        "device": match["name"],
                        "is_active": False,
                        "volume": match.get("volume_percent"),
                        "active_device": active,
                        "note": ("playback is on {!r}".format(active) if active
                                 else "nothing is currently playing"),
                    }, indent=2))
            else:
                st = sp.status()
                if not st:
                    print("no active playback")
                    return 0
                item = st.get("item") or {}
                dev = st.get("device") or {}
                print(json.dumps({
                    "device": dev.get("name"),
                    "is_playing": st.get("is_playing"),
                    "track": item.get("name"),
                    "artists": ", ".join(a["name"] for a in item.get("artists", [])),
                    "volume": dev.get("volume_percent"),
                }, indent=2))
    except LitheError as e:
        print("error: {}".format(e), file=sys.stderr)
        return 2
    return 0


# -- internet radio relay ---------------------------------------------------
#
# The UPnP renderer plays finite files but stalls forever on continuous live
# streams (no Content-Length, ICY metadata). The fix: a local relay that pulls
# the stream WITHOUT requesting ICY metadata and re-serves it with a length
# header, so the renderer treats it as a long finite file and plays it. The
# speaker fetches from this relay over the normal UPnP `play` path.

_RELAY_FAKE_LEN = "2147483647"  # ~2GB: "finite" enough that the renderer starts


def _local_ip_for(host):
    """The local IP this machine would use to reach `host` (for the relay URL)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _relay_handler(upstream_url):
    """Build an HTTP handler that proxies `upstream_url` as a length-bearing file."""

    class _Relay(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_headers(self, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", _RELAY_FAKE_LEN)
            self.send_header("Accept-Ranges", "none")
            self.end_headers()

        def do_HEAD(self):
            self._send_headers("audio/mpeg")

        def do_GET(self):
            try:  # no Icy-MetaData header -> server sends plain audio, no metadata
                up = urllib.request.urlopen(upstream_url, timeout=10)
            except (urllib.error.URLError, socket.timeout, OSError):
                self.send_response(502)
                self.end_headers()
                return
            ctype = up.headers.get("Content-Type", "audio/mpeg")
            if not ctype.startswith("audio/"):
                ctype = "audio/mpeg"
            self._send_headers(ctype)
            try:
                while True:
                    chunk = up.read(16384)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # speaker disconnected (stop/next) — expected
            finally:
                up.close()

        def log_message(self, *a):
            pass

    return _Relay


class _RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True  # rebind immediately after a stop (avoid TIME_WAIT EADDRINUSE)
    daemon_threads = True


def run_radio(speaker, url, proxy_host, proxy_port):
    """Relay a live stream `url` through a local server and play it on `speaker`.

    Blocks until Ctrl-C, then stops the speaker and shuts the relay down.
    """
    try:
        httpd = _RelayServer(("0.0.0.0", proxy_port), _relay_handler(url))
    except OSError as e:
        raise LitheError(
            "could not start relay on port {}: {} (try --proxy-port)".format(proxy_port, e)
        )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    relay_url = "http://{}:{}/".format(proxy_host, proxy_port)
    try:
        _print_timed("radio -> " + url, lambda: speaker.play(relay_url))
        print("streaming via {}  —  press Ctrl-C to stop".format(relay_url))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nstopping")
    finally:
        try:
            speaker.stop()
        except LitheError:
            pass
        httpd.shutdown()


# -- CLI --------------------------------------------------------------------

def _print_timed(label, fn):
    t0 = time.perf_counter()
    result = fn()
    ms = (time.perf_counter() - t0) * 1000.0
    print("{}  ({:.0f} ms)".format(label, ms))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="lithe", description="Low-latency UPnP driver for Lithe Audio WiFi V2 speakers."
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("LITHE_HOST"),
        help="Speaker IP or hostname (or set LITHE_HOST).",
    )
    parser.add_argument(
        "--port", type=int, default=None, help="Renderer control port (default: auto-discover)."
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="Request timeout (s)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "discover", help="Find media devices on this network (SSDP/UPnP + mDNS)."
    )
    sub.add_parser(
        "probe", help="Fingerprint --host across protocols to identify what it is."
    )
    sub.add_parser("info", help="Renderer metadata.")
    sub.add_parser("status", help="Playback state.")
    p_play = sub.add_parser("play", help="Play a stream URL.")
    p_play.add_argument("url")
    sub.add_parser("stop", help="Stop playback.")
    sub.add_parser("pause", help="Pause.")
    sub.add_parser("resume", help="Resume.")
    sub.add_parser("toggle", help="Toggle pause/resume.")
    sub.add_parser("next", help="Next track.")
    sub.add_parser("prev", help="Previous track.")
    p_seek = sub.add_parser("seek", help="Seek to position (seconds).")
    p_seek.add_argument("seconds", type=int)
    p_vol = sub.add_parser("vol", help="Set volume 0-100.")
    p_vol.add_argument("level", type=int)
    sub.add_parser("mute", help="Mute.")
    sub.add_parser("unmute", help="Unmute.")
    p_bench = sub.add_parser("bench", help="Measure control round-trip latency.")
    p_bench.add_argument("-n", type=int, default=20, help="Number of samples.")
    p_radio = sub.add_parser(
        "radio", help="Stream a live internet-radio URL via a local relay (handles ICY streams)."
    )
    p_radio.add_argument("url", help="Upstream stream URL (MP3 streams work best).")
    p_radio.add_argument(
        "--proxy-port", type=int, default=8088, help="Local relay port (default 8088)."
    )
    p_radio.add_argument(
        "--proxy-host",
        default=None,
        help="Local IP the speaker fetches from (default: auto-detected).",
    )

    # Shared options, attached to each spotify subcommand so they work in the
    # natural position (e.g. `spotify play <uri> --device "Guest WC Speaker"`).
    sp_common = argparse.ArgumentParser(add_help=False)
    sp_common.add_argument(
        "--client-id",
        default=os.environ.get("LITHE_SPOTIFY_CLIENT_ID"),
        help="Spotify app client id (or set LITHE_SPOTIFY_CLIENT_ID).",
    )
    sp_common.add_argument(
        "--device",
        default=os.environ.get("LITHE_SPOTIFY_DEVICE"),
        help="Target Connect device name, substring match (or LITHE_SPOTIFY_DEVICE). "
        "Default: the currently-active Spotify device.",
    )

    p_sp = sub.add_parser("spotify", help="Control Spotify Connect on the speaker (Premium).")
    sp_sub = p_sp.add_subparsers(dest="spcmd", required=True)
    sp_sub.add_parser("login", parents=[sp_common], help="Authorize via browser (one-time).")
    sp_sub.add_parser("devices", parents=[sp_common], help="List Connect devices.")
    p_sp_play = sp_sub.add_parser(
        "play", parents=[sp_common], help="Play on the target device (optional track/playlist URI/link)."
    )
    p_sp_play.add_argument("uri", nargs="?")
    sp_sub.add_parser("pause", parents=[sp_common], help="Pause.")
    sp_sub.add_parser("resume", parents=[sp_common], help="Resume.")
    sp_sub.add_parser("next", parents=[sp_common], help="Next track.")
    sp_sub.add_parser("prev", parents=[sp_common], help="Previous track.")
    p_sp_vol = sp_sub.add_parser("vol", parents=[sp_common], help="Set volume 0-100.")
    p_sp_vol.add_argument("level", type=int)
    sp_sub.add_parser("status", parents=[sp_common], help="Current playback state.")

    args = parser.parse_args(argv)

    if args.cmd == "spotify":
        return _spotify_cli(args)

    if args.cmd == "discover":
        results = discover()
        if not results:
            print(
                "No media devices found via SSDP/UPnP or mDNS. Are you on the same "
                "LAN/VLAN as the speakers? (multicast often does not cross VLANs)"
            )
            return 1
        for r in results:
            ctrl = " [UPnP :{}]".format(r["control_port"]) if r.get("renderer") else ""
            print("{:<16}  {}{}".format(r["ip"], r["name"], ctrl))
            print("                  platforms: {}".format(", ".join(r["platforms"]) or "?"))
            mfr, model = r.get("manufacturer", ""), r.get("model", "")
            if model.startswith(mfr):  # model often already includes the maker
                device = model
            else:
                device = "{} {}".format(mfr, model).strip()
            if device:
                print("                  device:    {}".format(device))
        return 0

    if not args.host:
        parser.error("--host is required (or set LITHE_HOST). Run 'discover' to find one.")

    if args.cmd == "probe":
        print(json.dumps(probe(args.host, timeout=args.timeout), indent=2))
        return 0

    sp = LitheSpeaker(args.host, timeout=args.timeout, port=args.port)

    try:
        if args.cmd == "info":
            print(json.dumps(sp.info(), indent=2))
        elif args.cmd == "status":
            print(json.dumps(sp.status(), indent=2))
        elif args.cmd == "play":
            _print_timed("play -> " + args.url, lambda: sp.play(args.url))
        elif args.cmd == "stop":
            _print_timed("stop", sp.stop)
        elif args.cmd == "pause":
            _print_timed("pause", sp.pause)
        elif args.cmd == "resume":
            _print_timed("resume", sp.resume)
        elif args.cmd == "toggle":
            _print_timed("toggle", sp.toggle)
        elif args.cmd == "next":
            _print_timed("next", sp.next)
        elif args.cmd == "prev":
            _print_timed("prev", sp.prev)
        elif args.cmd == "seek":
            _print_timed("seek {}s".format(args.seconds), lambda: sp.seek(args.seconds))
        elif args.cmd == "vol":
            _print_timed("vol {}".format(args.level), lambda: sp.volume(args.level))
        elif args.cmd == "mute":
            _print_timed("mute", lambda: sp.mute(True))
        elif args.cmd == "unmute":
            _print_timed("unmute", lambda: sp.mute(False))
        elif args.cmd == "bench":
            print(json.dumps(sp.bench(args.n), indent=2))
        elif args.cmd == "radio":
            proxy_host = args.proxy_host or _local_ip_for(args.host)
            if not proxy_host:
                raise LitheError(
                    "could not determine a local IP reachable by the speaker; pass --proxy-host"
                )
            run_radio(sp, args.url, proxy_host, args.proxy_port)
    except LitheError as e:
        print("error: {}".format(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
