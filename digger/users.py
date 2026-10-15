"""User info cache, user header, browse page and the Users tab."""

import base64
import collections
import time
import urllib.error
import urllib.parse
import urllib.request
import weakref

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .constants import ICON_SIZE
from .logger import log
from .util import build_browse_tree, describe_error, normalize_presence
from .icons import set_item_icon
from .asyncutil import deliver, run_async
from .widgets import (
    Placeholder, StatusDot, enable_row_toggle, make_sidebar_row, sidebar_action,
    sidebar_placeholder,
)
from .models import BrowseItem


class UserDirectory:
    """Cached user info/status lookups (hover tooltips and the Users tab)."""
    TTL = 120  # seconds
    PRESENCE = {0: "Offline", 1: "Away", 2: "Online"}

    STATUS_TTL = 60
    MAX_STATUS_ACTIVE = 4      # concurrent status requests
    MAX_STATUS_QUEUE = 200     # drop the oldest when scrolling fast

    def __init__(self, get_api):
        self.get_api = get_api
        self.cache = {}      # username -> (timestamp, data)
        self.loading = set()
        self.waiters = {}
        self.status_cache = {}               # lowercase name -> (timestamp, presence)
        self.status_waiters = {}
        self.status_queue = collections.deque()
        self.status_pending = set()
        self.status_active = 0
        self.textures = {}                   # username -> Gdk.Texture | None

    def texture(self, username, data):
        """Profile picture from a user-info response (slskd sends it base64 encoded)."""
        if username in self.textures:
            return self.textures[username]
        tex = None
        raw = (data or {}).get("picture")
        try:
            if isinstance(raw, str) and raw:
                raw = base64.b64decode(raw)
            elif isinstance(raw, list) and raw:
                raw = bytes(raw)
            else:
                raw = None
            if raw:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
        except Exception as e:  # noqa: BLE001
            log.debug("picture of %s unusable: %s", username, e)
        if data:                       # only cache once real info arrived
            self.textures[username] = tex
        return tex

    def reset(self):
        self.textures.clear()
        self.cache.clear()
        self.status_cache.clear()
        self.status_waiters.clear()
        self.status_queue.clear()
        self.status_pending.clear()

    # -- presence (cheap, one small request per user, throttled) --
    def peek_status(self, username):
        entry = self.status_cache.get(username.lower())
        if entry and time.time() - entry[0] < self.STATUS_TTL:
            return entry[1]
        return None

    def set_status(self, username, presence):
        key = username.lower()
        self.status_cache[key] = (time.time(), presence)
        for cb in self.status_waiters.pop(key, []):
            cb(presence)

    def status(self, username, callback):
        """callback(presence) once known; presence is '' if it can't be determined."""
        key = username.lower()
        cached = self.peek_status(username)
        if cached is not None:
            callback(cached)
            return
        self.status_waiters.setdefault(key, []).append(callback)
        if key in self.status_pending or not self.get_api():
            return
        self.status_pending.add(key)
        self.status_queue.append(username)
        while len(self.status_queue) > self.MAX_STATUS_QUEUE:
            old = self.status_queue.popleft().lower()
            self.status_pending.discard(old)
            self.status_waiters.pop(old, None)
        self._pump()

    def _pump(self):
        api = self.get_api()
        while api and self.status_active < self.MAX_STATUS_ACTIVE and self.status_queue:
            username = self.status_queue.popleft()
            self.status_active += 1

            def work(u=username):
                try:
                    return normalize_presence((api.user_status(u) or {}).get("presence"))
                except Exception as e:  # noqa: BLE001
                    log.debug("status(%s) failed: %s", u, e)
                    return ""

            def done(presence, u=username):
                self.status_active -= 1
                self.status_pending.discard(u.lower())
                self.set_status(u, presence)
                self._pump()

            run_async(work, done, lambda _e, u=username: done("", u))

    def peek(self, username):
        entry = self.cache.get(username)
        if entry and time.time() - entry[0] < self.TTL:
            return entry[1]
        return None

    def fetch(self, username, callback=None, force=False):
        data = None if force else self.peek(username)
        if data is not None:
            if callback:
                callback(data)
            return
        if callback:
            self.waiters.setdefault(username, []).append(callback)
        api = self.get_api()
        if not api or username in self.loading:
            return
        self.loading.add(username)

        def work():
            result = {}
            for call in (api.user_info, api.user_status):
                try:
                    result.update(call(username) or {})
                except Exception as e:  # noqa: BLE001
                    log.debug("%s(%s) failed: %s", call.__name__, username, e)
            return result

        def done(result):
            self.loading.discard(username)
            self.cache[username] = (time.time(), result)
            presence = normalize_presence(result.get("presence")) if result else ""
            if presence:
                self.set_status(username, presence)
            for cb in self.waiters.pop(username, []):
                cb(result)

        run_async(work, done, lambda _e: done({}))

    @classmethod
    def detail_lines(cls, d):
        lines = []
        presence = d.get("presence")
        if isinstance(presence, int):
            presence = cls.PRESENCE.get(presence)
        if presence:
            lines.append(f"Status: {presence}"
                         + (" · Privileged" if d.get("isPrivileged") else ""))
        parts = []
        if "uploadSlots" in d:
            parts.append(f"{d['uploadSlots']} upload slots")
        if "queueLength" in d:
            parts.append(f"queue {d['queueLength']}")
        if "hasFreeUploadSlot" in d:
            parts.append("free slot" if d["hasFreeUploadSlot"] else "no free slot")
        if parts:
            lines.append(" · ".join(parts))
        desc = " ".join((d.get("description") or "").split())
        if desc:
            lines.append(desc if len(desc) <= 240 else desc[:237] + "…")
        return lines

    def markup(self, username, extra=None, refresh=None):
        esc = GLib.markup_escape_text
        data = self.peek(username)
        lines = [f"<b>{esc(username)}</b>"]
        if extra:
            lines.append(esc(extra))
        if data is None:
            if username not in self.loading:
                self.fetch(username, (lambda _d: refresh()) if refresh else None)
            lines.append("<i>Loading details…</i>")
        elif not data:
            lines.append("<i>No details available (user may be offline)</i>")
        else:
            lines.extend(esc(line) for line in self.detail_lines(data))
        return "\n".join(lines)


class UserHeader(Gtk.Box):
    """Avatar (profile picture or person icon, fixed 48 px) with status dot, name,
    details and action buttons. Shared by the browse page and the chat page."""
    SIZE = 48
    compact = False                     # phone layout: smaller, basic stats only
    headers = weakref.WeakSet()

    @classmethod
    def set_compact(cls, compact):
        cls.compact = compact
        for h in list(cls.headers):
            h._apply_compact()

    def __init__(self, host, actions):
        super().__init__(spacing=12, margin_top=12, margin_bottom=8,
                         margin_start=12, margin_end=12)
        self.host = host
        self.basic = ""
        self.desc_text = ""
        UserHeader.headers.add(self)
        self.username = None
        self.gen = 0
        self.details = ""
        self.summary = ""
        self.address = ""

        self.avatar = Adw.Avatar(size=self.SIZE, show_initials=False,
                                 valign=Gtk.Align.START)
        self.dot = StatusDot(host.user_dir, size=14)
        self.dot.set_halign(Gtk.Align.END)
        self.dot.set_valign(Gtk.Align.END)
        overlay = Gtk.Overlay(child=self.avatar, valign=Gtk.Align.START)
        overlay.add_overlay(self.dot)
        self.append(overlay)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.title = Gtk.Label(xalign=0, ellipsize=3)
        self.title.add_css_class("title-3")
        self.stats = Gtk.Label(xalign=0, wrap=True)
        self.stats.add_css_class("dim-label")
        self.desc = Gtk.Label(xalign=0, wrap=True, lines=3, ellipsize=3,
                              selectable=True, visible=False)
        for w in (self.title, self.stats, self.desc):
            info.append(w)
        self.append(info)
        for btn in actions:
            btn.set_valign(Gtk.Align.START)
            btn.add_css_class("flat")
            self.append(btn)
        self._apply_compact()

    def _apply_compact(self):
        c = UserHeader.compact
        self.avatar.set_size(32 if c else self.SIZE)
        self.set_margin_top(6 if c else 12)
        self.set_margin_bottom(4 if c else 8)
        self.set_spacing(8 if c else 12)
        self.desc.set_visible(bool(self.desc_text) and not c)
        self._render()

    def set_user(self, username, force=False):
        if username is None:
            self.gen += 1
            self.username = None
            self.dot.set_user(None)
            return
        self.gen += 1
        gen = self.gen
        self.username = username
        self.details = self.summary = self.address = ""
        self.basic = self.desc_text = ""
        self.title.set_text(username)
        self.avatar.set_text(username)
        self.avatar.set_custom_image(None)
        self.desc.set_visible(False)
        self.stats.set_text("Loading details…")
        self.dot.set_user(username)
        self.host.user_dir.fetch(
            username, lambda d: self._fill(d) if gen == self.gen else None, force=force)
        api = self.host.api
        if api:
            def got(addr):
                if gen == self.gen and addr:
                    self.address = addr
                    self._render()
            run_async(lambda: api.user_endpoint(username), got,
                      lambda e: log.debug("endpoint lookup failed: %s", e))

    def reload(self, *_):
        if self.username:
            self.set_user(self.username, force=True)

    def set_summary(self, text):
        self.summary = text
        self._render()

    def _render(self):
        parts = ((self.basic,) if UserHeader.compact
                 else (self.details, self.summary, self.address))
        self.stats.set_text(" · ".join(x for x in parts if x)
                            or "No details available")

    def _fill(self, data):
        lines = UserDirectory.detail_lines(data) if data else []
        self.dot.set_user(self.username)
        self.avatar.set_custom_image(self.host.user_dir.texture(self.username, data))
        # first two lines (status, slots/queue) go in the stats label
        self.details = " · ".join(lines[:2])
        self.basic = lines[0] if lines else ""        # just the online status
        desc = lines[2] if len(lines) > 2 else ""
        self.desc_text = desc
        self.desc.set_text(desc)
        self.desc.set_visible(bool(desc) and not UserHeader.compact)
        self._render()


class UserPage(Gtk.Box):
    """One tab of the Users view: user details + browsable share tree."""

    def __init__(self, username, host):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.username = username
        self.host = host
        self.gen = 0

        refresh = Gtk.Button(icon_name="digger-refresh-symbolic", tooltip_text="Reload")
        refresh.connect("clicked", self.load)
        chat = Gtk.Button(icon_name="digger-chat-symbolic",
                          tooltip_text="Chat with this user")
        chat.connect("clicked", lambda *_: self.host.show_chat(self.username))
        self.header = UserHeader(host, [chat, refresh])
        self.header.set_user(username)
        self.append(self.header)

        self.pages = Gtk.Stack(vexpand=True,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        loading = Placeholder(
            None, f"Browsing {username}…",
            "Waiting for their shared files. This can take a while.")
        self.loading_ph = loading
        self.browse_bar = Gtk.ProgressBar(width_request=260, halign=Gtk.Align.CENTER)
        loading.set_child(self.browse_bar)
        self.error = Placeholder("digger-error-symbolic", "Couldn't browse files")
        retry = Gtk.Button(label="Try Again", halign=Gtk.Align.CENTER)
        retry.add_css_class("suggested-action")
        retry.add_css_class("pill")
        retry.connect("clicked", self.load)
        self.error.set_child(retry)

        self.root = Gio.ListStore(item_type=BrowseItem)
        self.tree = Gtk.TreeListModel.new(self.root, False, False,
                                          self._children, None)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup)
        factory.connect("bind", self._bind)
        factory.connect("unbind", self._unbind)
        lv = Gtk.ListView(model=Gtk.NoSelection(model=self.tree), factory=factory)
        enable_row_toggle(lv)
        lv.add_css_class("rich-list")

        self.pages.add_named(loading, "loading")
        self.pages.add_named(self.error, "error")
        self.pages.add_named(Gtk.ScrolledWindow(child=lv), "tree")
        self.append(self.pages)

    def _browse_progress(self, gen, st):
        if gen != self.gen or not isinstance(st, dict):
            return
        got, size = st.get("bytesTransferred"), st.get("size")
        pct = st.get("percentComplete")
        frac = None
        if isinstance(got, (int, float)) and isinstance(size, (int, float)) and size:
            frac = got / size
        elif isinstance(pct, (int, float)):
            frac = pct / 100 if pct > 1 else pct
        if frac is None:
            self.browse_bar.pulse()
            return
        frac = max(0.0, min(1.0, frac))
        self.browse_bar.set_fraction(frac)
        text = f"Receiving file list… {frac * 100:.0f}%"
        if isinstance(got, (int, float)) and got:
            text += f" · {GLib.format_size(int(got))}"
            if isinstance(size, (int, float)) and size:
                text += f" of {GLib.format_size(int(size))}"
        self.loading_ph.set_description(text)

    def dispose(self):
        self.gen += 1  # ignore any browse still in flight

    @staticmethod
    def _children(item, *_):
        return item.children if item.expandable else None

    def load(self, *_):
        api = self.host.api
        if not api:
            return
        self.gen += 1
        gen = self.gen
        self.pages.set_visible_child_name("loading")
        self.root.remove_all()
        log.info("Browsing %s", self.username)
        self.header.set_user(self.username, force=True)
        self.browse_bar.set_fraction(0)
        self.loading_ph.set_description(
            "Waiting for their shared files. This can take a while.")
        stop = self._stop_poll = {"v": False}

        def poll_progress():
            while not stop["v"] and gen == self.gen:
                time.sleep(1.0)
                if stop["v"] or gen != self.gen:
                    break
                try:
                    st = api.user_browse_status(self.username)
                except Exception:  # noqa: BLE001
                    st = None
                if st:
                    GLib.idle_add(deliver, lambda x: self._browse_progress(gen, x), st)

        own = self.host.is_self(self.username)
        self.own = own
        if not own:                      # the local listing has no network transfer
            run_async(poll_progress)

        def ok(tree):
            stop["v"] = True
            if gen != self.gen:
                return
            items = [BrowseItem(self.username, node=c) for c in
                     sorted(tree["dirs"].values(), key=lambda c: c["name"].lower())]
            items += [BrowseItem(self.username, file=f) for f in tree["files"]]
            if not items:
                self.error.set_icon_name("digger-folder-symbolic")
                self.error.set_title("Nothing shared")
                self.error.set_description(
                    "This user isn't sharing any files, or they are all locked.")
                self.pages.set_visible_child_name("error")
                return
            self.root.splice(0, 0, items)
            n = tree["count"]
            self.header.set_summary(f"{n} file{'s' if n != 1 else ''} · "
                                    f"{GLib.format_size(int(tree['size']))}")
            self.pages.set_visible_child_name("tree")

        def err(e):
            stop["v"] = True
            if gen != self.gen:
                return
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                # slskd answers 404 when it can't reach the user: they are offline
                log.info("%s appears to be offline", self.username)
                self.error.set_icon_name("digger-offline-symbolic")
                self.error.set_title("User is offline")
                self.error.set_description(
                    f"{self.username} appears to be offline, so their files can't "
                    "be browsed. Try again when they are back online.")
                self.host.user_dir.set_status(self.username, "Offline")
                self.header.dot.set_user(self.username)
            else:
                self.error.set_icon_name("digger-error-symbolic")
                self.error.set_title("Couldn't browse files")
                self.error.set_description(describe_error(e))
            self.pages.set_visible_child_name("error")

        # your own username: read your shares straight from slskd instead of the network
        fetch = api.shares_contents if own else (
            lambda: api.user_browse(self.username))
        run_async(lambda: build_browse_tree(fetch()), ok, err)

    # rows
    def _setup(self, _f, li):
        exp = Gtk.TreeExpander()
        row = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6,
                      margin_start=4, margin_end=12)
        icon = Gtk.Image(width_request=ICON_SIZE)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=2)
        t = Gtk.Label(xalign=0, ellipsize=2)
        s = Gtk.Label(xalign=0, ellipsize=3)
        s.add_css_class("dim-label")
        s.add_css_class("caption")
        labels.append(t)
        labels.append(s)
        btn = Gtk.Button(icon_name="digger-download-symbolic", valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        for w in (icon, labels, btn):
            row.append(w)
        exp.set_child(row)
        exp._w = (icon, t, s, btn)
        exp._hid = 0
        li.set_child(exp)

    def _bind(self, _f, li):
        exp, tl = li.get_child(), li.get_item()
        exp.set_list_row(tl)
        item = tl.get_item()
        icon, t, s, btn = exp._w
        set_item_icon(icon, item.kind, item.title)
        t.set_text(item.title)
        t.set_tooltip_text(item.title)
        s.set_text(item.subtitle)
        btn.set_tooltip_text("Download folder" if item.kind == "dir" else "Download file")
        btn.set_visible(not getattr(self, "own", False))   # no downloads from yourself
        exp._hid = btn.connect("clicked", self._on_download, item)

    def _unbind(self, _f, li):
        exp = li.get_child()
        if exp._hid:
            exp._w[3].disconnect(exp._hid)
            exp._hid = 0
        exp.set_list_row(None)

    def _on_download(self, _btn, item):
        self.host.queue_download(self.username, item.payload())


class UsersView(Gtk.Box):
    """Look up a user and browse their shared files.

    Open users are listed in the sidebar (self.sidebar), like recent searches;
    the selected user's details and share tree fill the content area."""

    def __init__(self, host):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.host = host
        self.pages = {}   # lowercase username -> UserPage
        self.rows = {}    # lowercase username -> sidebar row

        # sidebar: "Browse User" popover, then the list of open users
        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.entry = Gtk.Entry(placeholder_text="Username, then Enter", width_chars=24)
        self.entry.connect("activate", self._on_activate)
        self.pop = Gtk.Popover(child=Gtk.Box(
            margin_top=6, margin_bottom=6, margin_start=6, margin_end=6))
        self.pop.get_child().append(self.entry)
        self.pop.connect("show", lambda *_: self.entry.grab_focus())
        self.sidebar.append(sidebar_action(self.pop, "Browse User"))
        self.list = Gtk.ListBox()
        self.list.add_css_class("navigation-sidebar")
        self.list.set_placeholder(sidebar_placeholder("No users open"))
        self.list.connect("row-selected", self._on_row_selected)
        self.list.connect("row-activated", lambda *_: self.host._hide_sidebar_if_collapsed())
        self.sidebar.append(Gtk.ScrolledWindow(vexpand=True, child=self.list))

        # content
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(Placeholder(
            "digger-user-symbolic", "No user selected",
            "Select a user from the sidebar, or browse a new one."),
            "empty")
        self.stack.set_visible_child_name("empty")
        self.append(self.stack)

    def _on_activate(self, entry):
        name = entry.get_text().strip()
        if name:
            entry.set_text("")
            self.pop.popdown()
            self.open_user(name)

    def open_user(self, username):
        key = username.lower()
        created = key not in self.pages
        if created:
            page = UserPage(username, self.host)
            self.stack.add_named(page, "user:" + key)
            row, _badge = make_sidebar_row(
                username, "digger-user-symbolic",
                lambda k=key: self.close_user(k), "Close",
                dot_dir=self.host.user_dir)
            row._key = key
            self.list.append(row)
            self.pages[key], self.rows[key] = page, row
        self.list.select_row(self.rows[key])
        if created:
            self.pages[key].load()

    def _on_row_selected(self, _lb, row):
        if row is None:
            if not self.rows:
                self.stack.set_visible_child_name("empty")
            return
        self.stack.set_visible_child_name("user:" + row._key)
        self.host._hide_sidebar_if_collapsed()

    def close_user(self, key):
        row, page = self.rows.pop(key, None), self.pages.pop(key, None)
        if row is None:
            return
        was_selected = self.list.get_selected_row() is row
        self.list.remove(row)
        if page is not None:
            page.dispose()
            self.stack.remove(page)
        if not self.rows:
            self.stack.set_visible_child_name("empty")
        elif was_selected:
            self.list.select_row(list(self.rows.values())[-1])

    def stop(self):
        for key in list(self.rows):
            self.close_user(key)
