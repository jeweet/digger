"""Main window: rooms tab."""

import re
import time

from gi.repository import Adw, GLib, Gtk

from .logger import log
from .util import describe_error, normalize_presence
from .asyncutil import run_async
from .widgets import (
    StatusDot, clear_children, empty_page, make_sidebar_row, pane_separator,
    sidebar_action, sidebar_placeholder,
)
from .messages import make_compose_bar, make_message_view, render_bubbles


class RoomsMixin:
    # ---- rooms tab ------------------------------------------------------
    def _build_rooms(self):
        # sidebar: join popover + joined rooms
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                          margin_top=6, margin_bottom=6, margin_start=6,
                          margin_end=6)
        self.avail_filter = Gtk.SearchEntry(
            placeholder_text="Filter, or type a name and press Enter")
        self.avail_filter.connect("search-changed",
                                  lambda *_: self._fill_available())
        self.avail_filter.connect("activate", self._on_join_typed)
        self.avail_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.avail_list.add_css_class("navigation-sidebar")
        self.avail_list.connect("row-activated", self._on_avail_activated)
        pop_box.append(self.avail_filter)
        pop_box.append(Gtk.ScrolledWindow(
            child=self.avail_list, min_content_height=320,
            min_content_width=300, propagate_natural_height=False))
        self.join_pop = Gtk.Popover(child=pop_box)
        self.join_pop.connect("show", self._load_available)

        side.append(sidebar_action(self.join_pop, "Join Room"))

        self.room_list = Gtk.ListBox()
        self.room_list.add_css_class("navigation-sidebar")
        self.room_list.set_placeholder(sidebar_placeholder("No rooms joined"))
        self.room_list.connect("row-selected", self._on_room_selected)
        self.room_list.connect("row-activated",
                               lambda *_: self._hide_sidebar_if_collapsed())
        side.append(Gtk.ScrolledWindow(vexpand=True, child=self.room_list))

        # main area
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(spacing=8, margin_top=6, margin_bottom=6,
                       margin_start=12, margin_end=6)
        head.append(Gtk.Image(icon_name="digger-rooms-symbolic"))
        self.room_title = Gtk.Label(label="Join a room to start chatting",
                                    xalign=0, hexpand=True, ellipsize=3)
        self.room_title.add_css_class("title-4")
        head.append(self.room_title)

        self.users_content = Adw.ButtonContent(icon_name="digger-rooms-symbolic",
                                               label="0")
        self.users_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.users_list.add_css_class("navigation-sidebar")
        self.users_list.connect("row-activated", self._on_user_activated)
        self.member_entry = Gtk.Entry(
            placeholder_text="Add member (private rooms), Enter", margin_start=6,
            margin_end=6, margin_bottom=6,
            tooltip_text="Invite a user to this room if it is private")
        self.member_entry.connect("activate", self._on_add_member)
        ubox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        ubox.append(Gtk.ScrolledWindow(child=self.users_list, min_content_height=320,
                                       min_content_width=260))
        ubox.append(self.member_entry)
        self.users_pop = Gtk.Popover(child=ubox)
        self.users_btn = Gtk.MenuButton(popover=self.users_pop, sensitive=False,
                                        tooltip_text="Users in room (click to message)")
        self.users_btn.set_child(self.users_content)
        self.users_btn.add_css_class("flat")
        head.append(self.users_btn)

        # topic tickers: one short line per user, plus your own
        self.ticker_content = Adw.ButtonContent(icon_name="digger-list-symbolic",
                                                label="Ticker")
        self.ticker_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.ticker_list.add_css_class("navigation-sidebar")
        # slskd's REST API only lets a client SET its own ticker; it does not hand
        # out the tickers of other users, so this list normally stays empty.
        self.ticker_list.set_placeholder(sidebar_placeholder(
            "slskd does not share other users' tickers with clients"))
        self.ticker_list.connect("row-activated", self._on_ticker_activated)
        self.ticker_entry = Gtk.Entry(placeholder_text="Set your ticker, then Enter",
                                      hexpand=True)
        self.ticker_entry.connect("activate", self._on_set_ticker)
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        tbox.append(Gtk.ScrolledWindow(child=self.ticker_list, min_content_height=240,
                                       min_content_width=300))
        tbox.append(self.ticker_entry)
        self.ticker_entry.set_margin_start(6)
        self.ticker_entry.set_margin_end(6)
        self.ticker_entry.set_margin_bottom(6)
        self.ticker_pop = Gtk.Popover(child=tbox)
        self.ticker_pop.connect("show", lambda *_: self._load_tickers())
        self.ticker_btn = Gtk.MenuButton(popover=self.ticker_pop, sensitive=False,
                                         tooltip_text="Set your room ticker")
        self.ticker_btn.set_child(self.ticker_content)
        self.ticker_btn.add_css_class("flat")
        head.append(self.ticker_btn)
        main.append(head)

        self.room_scroll, self.room_msgs = make_message_view()
        main.append(self.room_scroll)
        bar, self.room_entry = make_compose_bar(self._on_room_send)
        main.append(pane_separator())
        main.append(bar)
        self.side_rooms = side
        self.rooms_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.rooms_stack.add_named(empty_page(
            "digger-rooms-symbolic", "No room selected",
            "Join a room from the sidebar to start chatting."), "empty")
        self.rooms_stack.add_named(main, "room")
        self.rooms_stack.set_visible_child_name("empty")
        return self.rooms_stack

    def _on_add_member(self, entry):
        api, room = self.api, self.room_cur
        name = entry.get_text().strip()
        if not api or not room or not name:
            return
        entry.set_text("")

        def ok(_r=None):
            self.toast(f"Added {name} to {room}")
            self._fetch_room(room)

        run_async(lambda: api.add_room_member(room, name), ok,
                  lambda e: self.toast(f"Could not add {name}: {describe_error(e)}"))

    # tickers
    def _load_tickers(self, *_):
        api, room = self.api, self.room_cur
        if not api or not room:
            return

        def ok(tickers):
            if room != self.room_cur or api is not self.api:
                return
            clear_children(self.ticker_list)
            me = (self.me or "").lower()
            tickers.sort(key=lambda t: (t[0].lower() != me, t[0].lower()))
            for user, message in tickers:
                row = Gtk.ListBoxRow(activatable=user.lower() != me)
                row._user = user
                box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                              margin_top=4, margin_bottom=4, margin_start=8,
                              margin_end=8)
                name = Gtk.Label(label="You" if user.lower() == me else user, xalign=0)
                name.add_css_class("caption-heading")
                text = Gtk.Label(label=message, xalign=0, wrap=True, max_width_chars=40,
                                 selectable=False)
                box.append(name)
                box.append(text)
                row.set_child(box)
                self.ticker_list.append(row)
            self.ticker_content.set_label(
                f"Tickers ({len(tickers)})" if tickers else "Ticker")

        run_async(lambda: api.room_tickers(room), ok,
                  lambda e: log.debug("ticker fetch failed: %s", e))

    def _on_ticker_activated(self, _lb, row):
        user = getattr(row, "_user", None)
        if user:
            self.ticker_pop.popdown()
            self.show_chat(user)

    def _on_set_ticker(self, entry):
        api, room = self.api, self.room_cur
        text = entry.get_text().strip()
        if not api or not room or not text:
            return
        entry.set_text("")

        def ok(_r=None):
            self.toast("Ticker set")
            self._load_tickers()

        run_async(lambda: api.set_ticker(room, text), ok,
                  lambda e: self.toast(describe_error(e)))

    # join popover
    def _load_available(self, *_):
        api = self.api
        if not api:
            return

        def ok(rooms):
            rooms = [r for r in rooms if isinstance(r, dict) and r.get("name")]
            rooms.sort(key=lambda r: r.get("userCount", 0), reverse=True)
            self.avail_data = rooms
            self._fill_available()

        run_async(api.rooms_available, ok,
                  lambda e: self.toast(describe_error(e)))
        self.avail_filter.grab_focus()

    def _fill_available(self):
        text = self.avail_filter.get_text().strip().lower()
        clear_children(self.avail_list)
        shown = 0
        for r in self.avail_data:
            if text and text not in r["name"].lower():
                continue
            row = Gtk.ListBoxRow()
            row._name = r["name"]
            box = Gtk.Box(spacing=8, margin_top=6, margin_bottom=6,
                          margin_start=8, margin_end=8)
            name = Gtk.Label(label=r["name"], xalign=0, hexpand=True, ellipsize=3)
            info = f"{r.get('userCount', 0)} users"
            if r.get("isPrivate"):
                info += " · private"
            cnt = Gtk.Label(label=info)
            cnt.add_css_class("dim-label")
            cnt.add_css_class("caption")
            box.append(name)
            box.append(cnt)
            row.set_child(box)
            self.avail_list.append(row)
            shown += 1
            if shown >= 100:
                break

    def _on_avail_activated(self, _lb, row):
        self._join_room(row._name)

    def _on_join_typed(self, entry):
        name = entry.get_text().strip()
        if name:
            self._join_room(name)

    def _join_room(self, name):
        api = self.api
        self.join_pop.popdown()
        self.avail_filter.set_text("")
        if name in self.room_rows:
            self.room_list.select_row(self.room_rows[name])
            return

        def ok(_):
            self.rooms_epoch += 1
            row = self.room_rows.get(name) or self._add_room_row(name)
            self.room_list.select_row(row)
            self.room_entry.grab_focus()

        run_async(lambda: api.join_room(name), ok,
                  lambda e: self.toast(describe_error(e)))

    # joined rooms
    def _add_room_row(self, name):
        row, badge = make_sidebar_row(
            name, "digger-rooms-symbolic",
            lambda: self._on_leave_room(name), "Leave room")
        row._name = name
        row._badge = badge
        self.room_list.append(row)
        self.room_rows[name] = row
        return row

    def _on_leave_room(self, name):
        api = self.api

        def ok(_):
            self.rooms_epoch += 1
            row = self.room_rows.pop(name, None)
            self.room_unread.pop(name, None)
            if row:
                self.room_list.remove(row)
            self._update_room_badges()
            if self.room_cur == name:
                self._reset_room_view()

        run_async(lambda: api.leave_room(name), ok,
                  lambda e: self.toast(describe_error(e)))

    def _on_room_selected(self, _lb, row):
        if row is None:
            return
        self.room_cur = row._name
        self.rooms_stack.set_visible_child_name("room")
        self.room_title.set_text(row._name)
        self.room_entry.set_sensitive(True)
        self.users_btn.set_sensitive(True)
        self.ticker_btn.set_sensitive(True)
        clear_children(self.ticker_list)
        self._load_tickers()
        clear_children(self.room_msgs)
        self.room_sig = None
        self.users_sig = None
        self._fetch_room(row._name)
        self._withdraw(f"room-{row._name}")
        self._mark_room_read(row._name)
        self._hide_sidebar_if_collapsed()

    def _reset_room_view(self):
        self.room_cur = None
        self.room_sig = None
        self.users_sig = None
        clear_children(self.room_msgs)
        clear_children(self.users_list)
        self.room_title.set_text("Join a room to start chatting")
        self.rooms_stack.set_visible_child_name("empty")
        self.room_entry.set_sensitive(False)
        self.users_btn.set_sensitive(False)
        self.ticker_btn.set_sensitive(False)
        self.ticker_content.set_label("Ticker")
        self.users_content.set_label("0")

    def _fetch_room(self, room):
        api = self.api

        def ok(res):
            if room == self.room_cur and api is self.api:
                self._render_room(res[0])
                self._update_room_users(res[1])

        run_async(lambda: (api.room_messages(room), api.room_users(room)), ok,
                  lambda e: self.toast(describe_error(e)))

    # polling
    def _start_rooms_poll(self):
        if self.rooms_timer:
            GLib.source_remove(self.rooms_timer)
        self.rooms_timer = 0
        self._poll_rooms()

    def _poll_rooms(self, *_):
        if not self.api:
            self.rooms_timer = 0
            return False
        if self.rooms_timer:
            GLib.source_remove(self.rooms_timer)
            self.rooms_timer = 0
        if self.rooms_busy:
            return False
        self.rooms_busy = True
        api, room, epoch = self.api, self.room_cur, self.rooms_epoch
        visible = self.viewstack.get_visible_child_name() == "rooms"
        watch = True      # unread counts need the messages of every room

        def work():
            names = [j.get("name") if isinstance(j, dict) else j
                     for j in api.rooms_joined()]
            msgs = users = None
            if room and visible and room in names:
                msgs = api.room_messages(room)
                users = api.room_users(room)
            extra = {}
            if watch:
                # The messages endpoint has no "since" filter, so fetching every
                # room each tick is the expensive part. Instead the visible room
                # is read as above and the others are checked round-robin, a few
                # per tick (each reply holds the recent history, so nothing is
                # missed between visits).
                if room and msgs is not None:
                    extra[room] = msgs
                others = [n for n in names if n != room or msgs is None]
                budget = 4 if len(names) <= 12 else 2
                if self._events_live():
                    dirty = [n for n in others if n in self.room_dirty]
                    unknown = self.room_dirty_unknown
                    self.room_dirty.clear()
                    self.room_dirty_unknown = False
                    others = dirty + [n for n in others if n not in dirty] \
                        if unknown else dirty
                    budget = 8 if unknown else max(len(dirty), 1)
                    if not dirty and not unknown:
                        others = [n for n in names if n != room or msgs is None]
                        budget = 1                  # slow safety-net rotation
                if others:
                    start = self.__dict__.get("room_rr", 0) % len(others)
                    batch = (others + others)[start:start + min(budget, len(others))]
                    self.room_rr = start + len(batch)
                    for n in batch:
                        try:
                            extra[n] = api.room_messages(n)
                        except Exception as e:  # noqa: BLE001
                            log.debug("room %s fetch failed: %s", n, e)
                        time.sleep(0.15)      # no bursts
            return names, msgs, users, extra

        def ok(res):
            self.rooms_busy = False
            if api is self.api:
                self._update_rooms(res[0], room, res[1], res[2], epoch)
                if watch:
                    try:
                        self._check_room_notifications(res[3])
                    except Exception:  # noqa: BLE001
                        log.exception("room unread/notification check failed")
            self._next_poll("rooms")

        def err(_e):
            self.rooms_busy = False
            self._next_poll("rooms", ok=False)

        run_async(work, ok, err)
        return False

    def _update_rooms(self, names, room, msgs, users, epoch):
        for n in names:
            if n and n not in self.room_rows:
                self._add_room_row(n)
        if epoch == self.rooms_epoch:  # no join/leave raced with this poll
            for n in list(self.room_rows):
                if n not in names:
                    self.room_list.remove(self.room_rows.pop(n))
                    self.room_unread.pop(n, None)
                    if n == self.room_cur:
                        self._reset_room_view()
        if room and room == self.room_cur:
            if msgs is not None:
                self._render_room(msgs)
            if users is not None:
                self._update_room_users(users)

    # rendering
    def _render_room(self, msgs):
        msgs = sorted(msgs, key=lambda m: m.get("timestamp", ""))[-300:]
        last = msgs[-1] if msgs else {}
        sig = (self.room_cur, len(msgs), last.get("timestamp"),
               last.get("username"), last.get("message"))
        if sig == self.room_sig:
            return
        first = self.room_sig is None or self.room_sig[0] != self.room_cur
        adj = self.room_scroll.get_vadjustment()
        at_bottom = first or \
            adj.get_value() >= adj.get_upper() - adj.get_page_size() - 40
        self.room_sig = sig
        names = self.my_names()
        mention = (re.compile(r"(?<!\w)(" + "|".join(re.escape(n) for n in names)
                              + r")(?!\w)", re.I) if names else None)
        render_bubbles(self.room_msgs, [
            (m.get("username", ""), m.get("timestamp"), m.get("message", ""),
             m.get("self") is True or self.is_self(m.get("username")))
            for m in msgs], show_author=True, mention=mention)

        if at_bottom:
            def scroll():
                a = self.room_scroll.get_vadjustment()
                a.set_value(a.get_upper() - a.get_page_size())
                return False
            GLib.timeout_add(60, scroll)

    def _update_room_users(self, users):
        names = sorted((u.get("username", "") for u in users if isinstance(u, dict)),
                       key=str.lower)
        self.users_content.set_label(str(len(names)))
        seen = {}
        for u in users:                 # room data already carries each user's status
            if isinstance(u, dict) and u.get("username"):
                presence = normalize_presence(u.get("status"))
                seen[u["username"]] = presence
                if presence:
                    self.user_dir.set_status(u["username"], presence)
        sig = (self.room_cur, tuple((n, seen.get(n, "")) for n in names))
        if sig == self.users_sig:
            return
        self.users_sig = sig
        clear_children(self.users_list)
        for n in names[:1000]:
            row = Gtk.ListBoxRow()
            row._name = n
            box = Gtk.Box(spacing=8, margin_top=6, margin_bottom=6,
                          margin_start=8, margin_end=8)
            dot = StatusDot(self.user_dir, size=10)
            dot.set_user(n, fetch=False)   # no per-user requests for big rooms
            box.append(dot)
            box.append(Gtk.Label(label=n, xalign=0, ellipsize=3))
            row.set_child(box)
            self.users_list.append(row)

    def _on_user_activated(self, _lb, row):
        self.users_pop.popdown()
        name = row._name
        entry = self.chat_rows.get(name)
        row_ = entry[0] if entry else self._add_chat_row(name)
        self.chat_list.select_row(row_)
        self.viewstack.set_visible_child_name("chat")

    def _on_room_send(self, *_):
        text = self.room_entry.get_text().strip()
        room, api = self.room_cur, self.api
        if not text or not room or not api:
            return
        self.room_entry.set_text("")

        def ok(_):
            self._fetch_room(room)

        run_async(lambda: api.send_room_message(room, text), ok,
                  lambda e: self.toast(describe_error(e)))
