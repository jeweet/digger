"""Bottom status bar."""

import time

from gi.repository import GLib, Gtk

from .util import fmt_speed
from .asyncutil import run_async


class StatusBar(Gtk.ActionBar):
    """Status bar for the content pane: connection, user, shares, speeds."""
    STATE_CLASSES = ("success", "warning", "error", "dim-label")

    def __init__(self, get_api, get_user):
        super().__init__()
        self.get_api, self.get_user = get_api, get_user
        self.timer = 0
        self.busy = False
        self.skip = 0
        self.slow = lambda: False
        self.app = {}
        self.reachable = True
        self.latency = None
        self.xfer = {"downloads": {}, "uploads": {}}

        def item(icon, tip=None):
            box = Gtk.Box(spacing=6)
            img = Gtk.Image(icon_name=icon, pixel_size=14)
            img.add_css_class("dim-label")
            lbl = Gtk.Label(xalign=0)
            lbl.add_css_class("caption")
            lbl.add_css_class("numeric")
            box.append(img)
            box.append(lbl)
            return box, img, lbl

        self.toast = lambda _t: None
        self.conn_box, self.dot, self.conn_lbl = item("digger-dot-symbolic")
        self.on_conn_state = lambda known, connected: None
        self.user_box, _i, self.user_lbl = item("digger-user-symbolic")
        self.shares_box, _i, self.shares_lbl = item("digger-folder-symbolic")
        self.down_box, _i, self.down_lbl = item("digger-down-symbolic")
        self.up_box, _i, self.up_lbl = item("digger-up-symbolic")

        self.down_q, self.up_q = Gtk.Label(), Gtk.Label()
        for q, box in ((self.down_q, self.down_box), (self.up_q, self.up_box)):
            for c in ("caption", "dim-label", "numeric"):
                q.add_css_class(c)
            box.append(q)

        self.update_lbl = Gtk.Label(label="Update available", visible=False)
        self.update_lbl.add_css_class("caption")
        self.update_lbl.add_css_class("accent")

        for w in (self.conn_box, self.user_box, self.shares_box, self.update_lbl):
            self.pack_start(w)
        self.pack_end(self.up_box)      # rightmost
        self.pack_end(self.down_box)
        self._render()
        self._render_speeds()

    # -- polling (application state, every 10 s) --
    def start(self):
        self.stop()
        self._poll()
        self.timer = GLib.timeout_add_seconds(10, self._poll)

    def stop(self):
        self.on_conn_state(False, False)
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.app, self.latency, self.reachable = {}, None, True
        self.xfer = {"downloads": {}, "uploads": {}}
        self._render()
        self._render_speeds()

    def _poll(self, *_):
        api = self.get_api()
        if not api:
            self.timer = 0
            return False
        if self.busy:
            return True
        if self.slow() and self.skip < 2:      # background: every ~30 s instead of 10
            self.skip += 1
            return True
        self.skip = 0
        self.busy = True

        def work():
            t0 = time.monotonic()
            data = api.application()
            return data, (time.monotonic() - t0) * 1000

        def ok(res):
            self.busy = False
            if api is self.get_api():
                self.app = res[0] if isinstance(res[0], dict) else {}
                self.latency, self.reachable = res[1], True
                self._render()

        def err(_e):
            self.busy = False
            if api is self.get_api():
                self.reachable = False
                self._render()

        run_async(work, ok, err)
        return True

    def set_transfer_stats(self, kind, stats):
        self.xfer[kind] = stats
        self._render_speeds()

    # -- rendering --
    def _set_state(self, cls):
        for c in self.STATE_CLASSES:
            self.dot.remove_css_class(c)
        self.dot.add_css_class(cls)

    def _render(self):
        a = self.app
        srv = a.get("server") or {}
        ver = a.get("version") or {}
        if not self.reachable:
            text, cls = "slskd unreachable", "error"
        elif not a:
            text, cls = "Connecting…", "dim-label"
        elif srv.get("isConnected") and srv.get("isLoggedIn"):
            text, cls = "Connected", "success"
        elif srv.get("isConnected"):
            text, cls = "Logging in…", "warning"
        else:
            text, cls = "Disconnected", "error"
        self.conn_lbl.set_text(text)
        self._set_state(cls)
        known = self.reachable and bool(a)
        self.on_conn_state(known, bool(srv.get("isConnected")))
        tip = [f"Soulseek server: {srv['address']}" if srv.get("address") else None,
               f"State: {srv['state']}" if srv.get("state") else None,
               f"slskd {ver['current']}" if ver.get("current") else None,
               f"API latency: {self.latency:.0f} ms" if self.latency is not None else None]
        self.conn_box.set_tooltip_text("\n".join(t for t in tip if t) or None)

        user = (a.get("user") or {}).get("username") or self.get_user() or "—"
        self.user_lbl.set_text(user)
        priv = ((a.get("user") or {}).get("privileges") or {}).get("isPrivileged")
        self.user_box.set_tooltip_text(
            f"Signed in as {user}" + (" (privileged)" if priv else ""))

        sh = a.get("shares") or {}
        files, dirs = sh.get("files"), sh.get("directories")
        progress = sh.get("progress")
        if sh.get("scanning"):
            pct = ""
            if isinstance(progress, (int, float)):
                pct = f" {progress * 100 if progress <= 1 else progress:.0f}%"
            self.shares_lbl.set_text(f"Scanning shares…{pct}")
        elif isinstance(files, int):
            self.shares_lbl.set_text(f"{files:,} files shared")
        else:
            self.shares_lbl.set_text("— shared")
        lines = []
        if isinstance(files, int) and isinstance(dirs, int):
            lines.append(f"{files:,} files in {dirs:,} folders")
        if sh.get("scanPending") or a.get("pendingShareRescan"):
            lines.append("Share rescan pending")
        if sh.get("faulted"):
            lines.append("Last share scan failed")
        self.shares_box.set_tooltip_text("\n".join(lines) or None)

        if ver.get("isUpdateAvailable"):
            self.update_lbl.set_visible(True)
            self.update_lbl.set_tooltip_text(
                f"slskd {ver.get('latest', 'a newer version')} is available "
                f"(running {ver.get('current', '?')})")
        else:
            self.update_lbl.set_visible(False)

    def _render_speeds(self):
        for kind, box, lbl, verb in (
                ("downloads", self.down_box, self.down_lbl, "Downloading"),
                ("uploads", self.up_box, self.up_lbl, "Uploading")):
            st = self.xfer.get(kind) or {}
            lbl.set_text(fmt_speed(st.get("speed", 0)))
            (self.down_q if kind == "downloads" else self.up_q).set_text(
                f"· {st.get('queued', 0)} queued")
            box.set_tooltip_text(
                f"{verb}: {st.get('active', 0)} active · {st.get('queued', 0)} queued\n"
                f"{st.get('total', 0)} transfers listed\n"
                f"This session: {GLib.format_size(int(st.get('session', 0)))}")
