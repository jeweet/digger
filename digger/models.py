"""Data models for search results, transfers and browse trees."""

import collections

from gi.repository import Gio, GLib, GObject

from .util import audio_parts, basename, collect_files, fmt_timespan, split_path
from .messages import fmt_bubble_time


class FileItem(GObject.Object):
    def __init__(self, username, file):
        super().__init__()
        self.username = username
        self.filename = file.get("filename", "")
        self.title = split_path(self.filename)[1]
        self.size = file.get("size", 0)
        self.bitrate = file.get("bitRate")
        self.length = file.get("length")
        self.audio = audio_parts(file, self.filename)

    @property
    def subtitle(self):
        parts = [GLib.format_size(self.size)] + self.audio
        return " · ".join(parts)


DIR_CACHE = collections.OrderedDict()   # (user, folder) -> complete file list


def dir_cache_key(username, path):
    return (username.lower(), path.lower().replace("/", "\\"))


class DirItem(GObject.Object):
    """A folder in the search results. It starts with the files that matched; when
    it is first opened the complete folder is fetched from the user."""

    def __init__(self, username, path, files):
        super().__init__()
        self.username = username
        self.path = path
        self.loading = False
        self.loaded = False
        self.files = []
        self.size = 0
        self.children = Gio.ListStore(item_type=FileItem)
        cached = DIR_CACHE.get(dir_cache_key(username, path))
        if cached is not None:
            self.set_files([FileItem(username, f) for f in cached])
            self.loaded = True
        else:
            self.set_files(files)

    @property
    def title(self):
        return split_path(self.path)[1] or self.path or "/"

    def set_files(self, files):
        self.files = sorted(files, key=lambda f: f.title.lower())
        self.size = sum(f.size for f in self.files)
        self.children.splice(0, self.children.get_n_items(), self.files)

    @property
    def subtitle(self):
        n = len(self.files)
        return f"{n} file{'s' if n != 1 else ''} · {GLib.format_size(self.size)}"


class SearchFilter:
    """Client-side result filter using the slskd web UI's filter syntax.

    word / -word          path must contain / must not contain
    iscbr / isvbr         constant / variable bit rate
    islossless / islossy  lossless (has bit depth + sample rate) / lossy
    minbitrate:320        kbps          minbitdepth:24
    minfilesize:10        megabytes     minlength:300 (seconds)
    minfilesinfolder:8
    """
    ALIASES = {
        "minbitrate": "min_bitrate", "minbr": "min_bitrate",
        "minbitdepth": "min_bitdepth", "minbd": "min_bitdepth",
        "minfilesize": "min_filesize", "minfs": "min_filesize",
        "minlength": "min_length", "minlen": "min_length",
        "minfilesinfolder": "min_files_in_folder", "minfif": "min_files_in_folder",
    }
    FLAGS = {"iscbr": "is_cbr", "isvbr": "is_vbr",
             "islossless": "is_lossless", "islossy": "is_lossy"}

    def __init__(self, text="", hide_no_slots=False):
        self.include, self.exclude = [], []
        self.is_cbr = self.is_vbr = self.is_lossless = self.is_lossy = False
        self.min_bitrate = self.min_bitdepth = self.min_filesize = 0.0
        self.min_length = self.min_files_in_folder = 0.0
        self.hide_no_slots = hide_no_slots
        for tok in (text or "").lower().split():
            key, _, val = tok.partition(":")
            if ":" in tok and key in self.ALIASES:
                try:
                    setattr(self, self.ALIASES[key], float(val))
                except ValueError:
                    pass          # still typing, e.g. "minbitrate:"
            elif tok in self.FLAGS:
                setattr(self, self.FLAGS[tok], True)
            elif "|" in tok and all(p in self.FLAGS for p in tok.split("|")):
                continue          # "iscbr|isvbr" style alternatives: no filter
            elif tok.startswith("-") and len(tok) > 1:
                self.exclude.append(tok[1:])
            else:
                self.include.append(tok)

    def match_file(self, f):
        name = (f.get("filename") or "").lower()
        if any(t not in name for t in self.include):
            return False
        if any(t in name for t in self.exclude):
            return False
        bitrate = f.get("bitRate") or 0
        depth = f.get("bitDepth") or 0
        rate = f.get("sampleRate") or 0
        vbr = f.get("isVariableBitRate")
        lossless = bool(depth and rate)   # only lossless formats carry both
        if self.is_vbr and vbr is not True:
            return False
        if self.is_cbr and not (bitrate and vbr is False):
            return False
        if self.is_lossless and not lossless:
            return False
        if self.is_lossy and lossless:
            return False
        if self.min_bitrate and bitrate < self.min_bitrate:
            return False
        if self.min_bitdepth and depth < self.min_bitdepth:
            return False
        if self.min_filesize and (f.get("size") or 0) < self.min_filesize * 1024 * 1024:
            return False
        if self.min_length and (f.get("length") or 0) < self.min_length:
            return False
        return True


class UserItem(GObject.Object):
    def __init__(self, response, flt=None):
        super().__init__()
        self.username = response.get("username", "?")
        self.slots = response.get("hasFreeUploadSlot", False)
        self.queue = response.get("queueLength", 0)
        self.speed = response.get("uploadSpeed", 0)

        groups = {}
        for f in response.get("files", []):
            if flt is not None and not flt.match_file(f):
                continue
            item = FileItem(self.username, f)
            groups.setdefault(split_path(item.filename)[0], []).append(item)
        dirs = [DirItem(self.username, path, files)
                for path, files in sorted(groups.items(),
                                          key=lambda kv: kv[0].lower())]
        if flt is not None and flt.min_files_in_folder:
            dirs = [d for d in dirs if len(d.files) >= flt.min_files_in_folder]
        self.file_count = sum(len(d.files) for d in dirs)
        self.dir_count = len(dirs)
        self.children = Gio.ListStore(item_type=DirItem)
        self.children.splice(0, 0, dirs)

    @property
    def tip_extra(self):
        if self.speed:
            return f"Upload speed: {GLib.format_size(int(self.speed))}/s"
        return None

    def sort_key(self, mode="best"):
        name = self.username.lower()
        if mode == "speed_desc":
            return (-self.speed, name)
        if mode == "speed_asc":
            return (self.speed, name)
        if mode == "queue_asc":
            return (self.queue, name)
        if mode == "queue_desc":
            return (-self.queue, name)
        if mode == "files_desc":
            return (-self.file_count, name)
        return (not self.slots, -self.speed, name)   # free slot, then fastest

    @property
    def subtitle(self):
        parts = [f"{self.file_count} files in {self.dir_count} "
                 f"folder{'s' if self.dir_count != 1 else ''}"]
        if self.speed:
            parts.append(f"{GLib.format_size(self.speed)}/s")
        parts.append("free slot" if self.slots else f"queue {self.queue}")
        return " · ".join(parts)


def make_user_item(response, flt):
    """UserItem for a search response after filtering, or None if nothing is left."""
    if flt.hide_no_slots and not response.get("hasFreeUploadSlot"):
        return None
    user = UserItem(response, flt)
    return user if user.file_count else None


class TxItem(GObject.Object):
    """Row in the transfers tree. Emits 'changed' when its data was updated."""
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}
    kind = "file"
    title = ""
    tooltip = None

    def leaves(self):
        return [self]

    @property
    def fraction(self):
        leaves = self.leaves()
        size = sum(f.size for f in leaves)
        if not size:
            return 1.0 if leaves and all(f.completed for f in leaves) else 0.0
        return max(0.0, min(1.0, sum(f.transferred for f in leaves) / size))

    @property
    def subtitle(self):
        leaves = self.leaves()
        n = len(leaves)
        done = sum(1 for f in leaves if f.completed)
        active = [f for f in leaves if f.active]
        parts = [f"{n} file{'s' if n != 1 else ''}",
                 GLib.format_size(int(sum(f.size for f in leaves)))]
        if done:
            parts.append(f"{done} completed")
        if active:
            parts.append(f"{len(active)} active")
            speed = sum(f.speed for f in active)
            if speed:
                parts.append(f"{GLib.format_size(int(speed))}/s")
        return " · ".join(parts)


class TxFile(TxItem):
    kind = "file"

    def __init__(self, data):
        super().__init__()
        self.position = None          # place in the remote queue (downloads)
        self.set_data(data)

    def detail_markup(self):
        """Hover details; everything slskd reports for this transfer."""
        esc = GLib.markup_escape_text
        d = self.data
        lines = [f"<b>{esc(self.title)}</b>", f"<small>{esc(self.filename)}</small>",
                 f"State: {esc(self.state or '?')}"]
        if self.position is not None and self.state.startswith("Queued"):
            lines.append(f"Queue position: {self.position}")
        size = GLib.format_size(int(self.size))
        got = GLib.format_size(int(self.transferred))
        lines.append(f"Size: {size}" + (f" · transferred {got}" if self.transferred
                                        and not self.state.startswith("Completed, Succ")
                                        else ""))
        if self.speed:
            lines.append(f"Average speed: {GLib.format_size(int(self.speed))}/s")
        for label, key in (("Elapsed", "elapsedTime"), ("Remaining", "remainingTime")):
            span = fmt_timespan(d.get(key))
            if span and span != "0:00:00":
                lines.append(f"{label}: {span}")
        for label, key in (("Requested", "requestedAt"), ("Enqueued", "enqueuedAt"),
                           ("Started", "startedAt"), ("Ended", "endedAt")):
            if d.get(key):
                lines.append(f"{label}: {esc(fmt_bubble_time(d[key]))}")
        if d.get("exception"):
            lines.append(f"<b>Error:</b> {esc(str(d['exception']))[:300]}")
        return "\n".join(lines)

    def set_data(self, d):
        self.data = d
        self.id = d.get("id")
        self.username = d.get("username", "")
        self.filename = d.get("filename", "")
        self.title = split_path(self.filename)[1]
        self.tooltip = self.filename
        self.size = d.get("size", 0) or 0
        self.state = d.get("state", "") or ""
        self.pct = d.get("percentComplete", 0) or 0
        self.speed = d.get("averageSpeed", 0) or 0

    @property
    def completed(self):
        return self.state.startswith("Completed")

    @property
    def active(self):
        return self.state == "InProgress"

    @property
    def transferred(self):
        if self.state.startswith("Completed, Succeeded"):
            return self.size
        t = self.data.get("bytesTransferred")
        return t if t is not None else self.size * self.pct / 100

    @property
    def subtitle(self):
        parts = [self.state, f"{self.fraction * 100:.0f}%",
                 GLib.format_size(int(self.size))]
        if self.active and self.speed:
            parts.append(f"{GLib.format_size(int(self.speed))}/s")
        if self.position is not None and self.state.startswith("Queued"):
            parts.append(f"position {self.position} in queue")
        if self.data.get("removed"):
            parts.append("removed")
        return " · ".join(p for p in parts if p)


class TxDir(TxItem):
    kind = "dir"

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.title = split_path(path)[1] or path or "/"   # path stays in the tooltip
        self.sort_key = path
        self.tooltip = path
        self.files = {}
        self.children = Gio.ListStore(item_type=TxFile)

    def leaves(self):
        return list(self.files.values())


class TxUser(TxItem):
    kind = "user"

    def __init__(self, username):
        super().__init__()
        self.title = username
        self.dirs = {}
        self.children = Gio.ListStore(item_type=TxDir)

    def leaves(self):
        return [f for d in self.dirs.values() for f in d.files.values()]


def _cmp_by(keyfn):
    def cmp(a, b, *_):
        ka, kb = keyfn(a), keyfn(b)
        return (ka > kb) - (ka < kb)
    return cmp


cmp_tx = _cmp_by(lambda x: getattr(x, "sort_key", x.title).lower())


class BrowseItem(GObject.Object):
    """Folder or file in a user's share. Children are created lazily."""

    def __init__(self, username, node=None, file=None):
        super().__init__()
        self.username = username
        self.node = node
        self.file = file
        self.kind = "dir" if node is not None else "file"
        self._children = None

    @property
    def title(self):
        return self.node["name"] if self.node is not None else basename(self.file["filename"])

    @property
    def expandable(self):
        return self.node is not None and bool(self.node["dirs"] or self.node["files"])

    @property
    def subtitle(self):
        if self.node is not None:
            n = self.node["count"]
            return f"{n} file{'s' if n != 1 else ''} · {GLib.format_size(int(self.node['size']))}"
        f = self.file
        parts = [GLib.format_size(int(f["size"]))] + audio_parts(f, f["filename"])
        return " · ".join(parts)

    @property
    def children(self):
        if self._children is None:
            n = self.node
            dirs = [BrowseItem(self.username, node=c) for c in
                    sorted(n["dirs"].values(), key=lambda c: c["name"].lower())]
            files = [BrowseItem(self.username, file=f) for f in
                     sorted(n["files"], key=lambda f: basename(f["filename"]).lower())]
            self._children = Gio.ListStore(item_type=BrowseItem)
            self._children.splice(0, 0, dirs + files)
        return self._children

    def payload(self):
        files = collect_files(self.node) if self.node is not None else [self.file]
        return [{"filename": f["full"], "size": f["size"]} for f in files]
