"""Pure helper functions (formatting, paths, transfer states)."""

import os
import re
import urllib.error
import urllib.parse
import urllib.request

from gi.repository import GLib


def normalize_presence(p):
    """slskd presence (string or number) -> 'Online' | 'Away' | 'Offline' | ''."""
    if isinstance(p, int):
        p = {0: "Offline", 1: "Away", 2: "Online"}.get(p)
    return p.capitalize() if isinstance(p, str) and p else ""


def fmt_speed(bps):
    return f"{GLib.format_size(int(bps))}/s" if bps and bps >= 1 else "0 B/s"


def fmt_timespan(value):
    """slskd TimeSpan ('[d.]hh:mm:ss[.fff]') -> '[Nd ]H:MM:SS'."""
    m = re.match(r"^(?:(\d+)\.)?(\d+):(\d+):(\d+)(?:\.\d+)?$", value or "")
    if not m:
        return ""
    d, h, mi, sec = m.groups()
    text = f"{int(h)}:{mi}:{sec}"
    return f"{d}d {text}" if d else text


def describe_error(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return "Authentication failed (401)"
        return f"Server error: HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"Cannot connect: {e.reason}"
    return str(e)


def basename(path):
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def split_path(filename):
    i = max(filename.rfind("\\"), filename.rfind("/"))
    return (filename[:i], filename[i + 1:]) if i >= 0 else ("", filename)


def audio_parts(f, name=""):
    """Size-independent audio details of a file dict: format, bit depth / sample
    rate (lossless: flac, wav, ...), bit rate (lossy: mp3, ...) and duration."""
    depth, rate = f.get("bitDepth"), f.get("sampleRate")
    bitrate, length = f.get("bitRate"), f.get("length")
    parts = []
    if depth or rate or bitrate or length:
        ext = os.path.splitext(name or f.get("filename", ""))[1].lstrip(".")
        if ext and (depth or rate or bitrate):
            parts.append(ext.upper())
    if depth and rate:
        parts.append(f"{depth}-bit / {rate / 1000:g} kHz")
    elif rate:
        parts.append(f"{rate / 1000:g} kHz")
    elif depth:
        parts.append(f"{depth}-bit")
    if bitrate:
        parts.append(f"{bitrate} kbps" + (" VBR" if f.get("isVariableBitRate") else ""))
    if length:
        parts.append(fmt_length(length))
    return parts


def fmt_length(seconds):
    if not seconds:
        return ""
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def tx_category(state):
    """Group slskd transfer states: active | queued | completed | errored | cancelled."""
    st = state or ""
    if st == "InProgress":
        return "active"
    if st.startswith("Completed"):
        if "Succeeded" in st:
            return "completed"
        if "Cancelled" in st:
            return "cancelled"
        return "errored"      # Errored, TimedOut, Rejected, Aborted, ...
    return "queued"           # Queued*, Requested, Initializing


def tx_cat(f):
    """Status category of a transfer dict; removed transfers form their own group."""
    return "removed" if f.get("removed") else tx_category(f.get("state"))


def build_browse_tree(response):
    """Turn slskd's flat browse response into a nested folder tree (plain dicts)."""
    def new_node(name, path):
        return {"name": name, "path": path, "dirs": {}, "files": [],
                "count": 0, "size": 0}

    root = new_node("", "")
    directories = response.get("directories") if isinstance(response, dict) else response
    for d in directories or []:
        dirname = d.get("name", "")
        node, path = root, ""
        for part in [p for p in re.split(r"[\\/]", dirname) if p]:
            path = f"{path}\\{part}" if path else part
            node = node["dirs"].setdefault(part, new_node(part, path))
        for f in d.get("files", []) or []:
            name = f.get("filename", "")
            full = name if ("\\" in name or "/" in name) else f"{dirname}\\{name}"
            node["files"].append({"filename": name, "full": full,
                                  "size": f.get("size", 0) or 0,
                                  "bitRate": f.get("bitRate"),
                                  "bitDepth": f.get("bitDepth"),
                                  "sampleRate": f.get("sampleRate"),
                                  "isVariableBitRate": f.get("isVariableBitRate"),
                                  "length": f.get("length")})

    def aggregate(node):
        count = len(node["files"])
        size = sum(f["size"] for f in node["files"])
        for child in node["dirs"].values():
            c, sz = aggregate(child)
            count += c
            size += sz
        node["count"], node["size"] = count, size
        return count, size

    aggregate(root)
    return root


def collect_files(node):
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        out.extend(n["files"])
        stack.extend(n["dirs"].values())
    return out
