"""Main window: events feed and background polling."""

import json
import time

from gi.repository import GLib

from .logger import log
from .util import basename
from .asyncutil import run_async


class EventsMixin:
    # ---- events feed: wakes the pollers when something actually happened ----
    def _events_live(self):
        return self.ev_ok and time.time() - self.ev_last_ok < 30

    def _start_events(self):
        if self.ev_timer:
            GLib.source_remove(self.ev_timer)
            self.ev_timer = 0
        self.ev_ok, self.ev_last_ok, self.ev_seen, self.ev_first = False, 0, set(), True
        self.ev_fails = 0
        self._poll_events()

    def _stop_events(self):
        if self.ev_timer:
            GLib.source_remove(self.ev_timer)
            self.ev_timer = 0
        self.ev_ok = False
        self.room_dirty.clear()
        self.room_dirty_unknown = False

    @staticmethod
    def _event_data(ev):
        data = ev.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                data = {}
        return data if isinstance(data, dict) else {}

    def _poll_events(self):
        self.ev_timer = 0
        api = self.api
        if not api or self.ev_busy:
            return False
        self.ev_busy = True

        def ts(e):
            return str(e.get("timestamp") or "")

        def work():
            page, total = api.events(0, 50)
            # newest-last ordering: the fresh events are at the far end
            if len(page) >= 2 and ts(page[0]) < ts(page[-1]) and total > len(page):
                tail, _t = api.events(max(0, total - 50), 50)
                page = page + tail
            return page

        def ok(events):
            self.ev_busy = False
            if api is not self.api:
                return
            self.ev_ok, self.ev_last_ok, self.ev_fails = True, time.time(), 0
            fresh = [e for e in events if e.get("id") not in self.ev_seen]
            self.ev_seen.update(e.get("id") for e in events)
            if len(self.ev_seen) > 2000:
                self.ev_seen = {e.get("id") for e in events}
            if self.ev_first:                 # first look: just remember history
                self.ev_first = False
            else:
                self._dispatch_events(fresh)
            self._schedule_events()

        def err(e):
            self.ev_busy = False
            self.ev_ok = False
            self.ev_fails += 1
            log.debug("events poll failed: %s", e)
            if api is self.api:
                self._schedule_events()

        run_async(work, ok, err)
        return False

    def _schedule_events(self):
        if not self.api:
            return
        base = 4 if self._window_focused() else 10
        if self.ev_fails >= 3:               # events unsupported or erroring: back off
            base = 60
        sec = min(60, base * 2 ** min(self.ev_fails, 3))
        self.ev_timer = GLib.timeout_add_seconds(sec, self._poll_events)

    def _notify_downloads(self, files, dirs):
        """One notification per burst of finished downloads."""
        if not self.cfg.get("notify_downloads") or self._window_focused():
            return
        if dirs:
            n = len(dirs)
            title = "Folder downloaded" if n == 1 else f"{n} folders downloaded"
            body = "\n".join(dirs[:3])
        elif files:
            n = len(files)
            title = "Download complete" if n == 1 else f"{n} downloads complete"
            body = "\n".join(files[:3]) + (f"\n… and {n - 3} more" if n > 3 else "")
        else:
            return
        self.notify("downloads", title, body, "app.open-downloads", "")

    def _dispatch_events(self, events):
        chat = rooms = False
        done_files, done_dirs = [], []
        for ev in events:
            kind = str(ev.get("type") or "")
            data = self._event_data(ev)
            if "PrivateMessage" in kind:
                chat = True
            elif "RoomMessage" in kind:
                room = (data.get("roomName") or data.get("room") or
                        data.get("RoomName") or "")
                if room:
                    self.room_dirty.add(room)
                else:
                    self.room_dirty_unknown = True
                rooms = True
            elif "Download" in kind:
                self.downloads_view._poll()
                tr = data.get("transfer") if isinstance(data.get("transfer"), dict) else {}
                who = tr.get("username") or data.get("username") or ""
                if "Directory" in kind:
                    path = (data.get("remoteDirectoryName") or data.get("localDirectoryName")
                            or "")
                    name = basename(path) if path else "folder"
                    done_dirs.append(f"{name} · {who}" if who else name)
                elif "Complete" in kind:
                    state = tr.get("state") or ""
                    if state and "Succeeded" not in state:
                        continue                     # failed / cancelled: no fanfare
                    path = (data.get("remoteFilename") or tr.get("filename")
                            or data.get("localFilename") or "")
                    name = basename(path) if path else "file"
                    done_files.append(f"{name} · {who}" if who else name)
            elif "Upload" in kind:
                self.uploads_view._poll()
        if events:
            log.debug("%d new server event(s)", len(events))
        self._notify_downloads(done_files, done_dirs)
        if chat and not self.chat_busy:
            self._poll_cb("chat")
        if rooms and not self.rooms_busy:
            self._poll_cb("rooms")

    def _poll_now(self, *_):
        """Window got focus: refresh right away instead of waiting for the timer."""
        if self.api and self._window_focused():
            self._load_searches()
            self.status_bar.skip = 2
            self.status_bar._poll()
            for view in (self.downloads_view, self.uploads_view):
                if view.timer:
                    view._poll()
            if not self.ev_busy and not self.ev_timer:
                self._poll_events()
            for kind in ("chat", "rooms"):
                if not getattr(self, f"{kind}_busy", False):
                    self._poll_cb(kind)
