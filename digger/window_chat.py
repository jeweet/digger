"""Main window: private chat tab."""

from gi.repository import GLib, Gtk

from .util import describe_error
from .asyncutil import run_async
from .widgets import (
    empty_page, make_sidebar_row, pane_separator, sidebar_action,
    sidebar_placeholder,
)
from .messages import make_compose_bar, make_message_view, render_bubbles
from .users import UserHeader


class ChatMixin:
    # ---- chat tab -------------------------------------------------------
    def _build_chat(self):
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.new_chat_entry = Gtk.Entry(placeholder_text="Username, then Enter",
                                        width_chars=24)
        self.new_chat_entry.connect("activate", self._on_new_chat)
        self.new_chat_pop = Gtk.Popover(child=Gtk.Box(
            margin_top=6, margin_bottom=6, margin_start=6, margin_end=6))
        self.new_chat_pop.get_child().append(self.new_chat_entry)
        self.new_chat_pop.connect("show",
                                  lambda *_: self.new_chat_entry.grab_focus())
        side.append(sidebar_action(self.new_chat_pop, "New Conversation"))

        self.chat_list = Gtk.ListBox()
        self.chat_list.add_css_class("navigation-sidebar")
        self.chat_list.set_placeholder(sidebar_placeholder("No conversations"))
        self.chat_list.connect("row-selected", self._on_chat_selected)
        self.chat_list.connect("row-activated",
                               lambda *_: self._hide_sidebar_if_collapsed())
        side.append(Gtk.ScrolledWindow(vexpand=True, child=self.chat_list))

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        browse_btn = Gtk.Button(icon_name="digger-folder-open-symbolic",
                                tooltip_text="Browse this user's files")
        browse_btn.connect("clicked", lambda *_: self.chat_user
                           and self.browse_user(self.chat_user))
        refresh_btn = Gtk.Button(icon_name="digger-refresh-symbolic",
                                 tooltip_text="Reload user details")
        refresh_btn.connect("clicked", lambda *_: self.chat_header.reload())
        self.chat_header = UserHeader(self, [browse_btn, refresh_btn])
        main.append(self.chat_header)

        self.msg_scroll, self.msg_list = make_message_view()
        main.append(self.msg_scroll)
        bar, self.msg_entry = make_compose_bar(self._on_send)
        main.append(pane_separator())
        main.append(bar)
        self.side_chat = side
        self.chat_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.chat_stack.add_named(empty_page(
            "digger-chat-symbolic", "No conversation selected",
            "Select a conversation from the sidebar, or start a new one."), "empty")
        self.chat_stack.add_named(main, "chat")
        self.chat_stack.set_visible_child_name("empty")
        return self.chat_stack

    def _start_chat_poll(self):
        if self.chat_timer:
            GLib.source_remove(self.chat_timer)
        self.chat_timer = 0
        self._poll_chat()

    # Adaptive polling: fast while you look at the tab, slow in the background,
    # exponential back-off while the server is failing. Each poll schedules the
    # next one only when it completes, so requests can never pile up.
    def _next_poll(self, kind, ok=True):
        fails = self.__dict__.setdefault("poll_fails", {})
        fails[kind] = 0 if ok else fails.get(kind, 0) + 1
        old = getattr(self, f"{kind}_timer", 0)
        if old:
            GLib.source_remove(old)
            setattr(self, f"{kind}_timer", 0)
        if not self.api:
            return
        focused = self._window_focused()
        on_tab = self.viewstack.get_visible_child_name() == kind
        if self._events_live():
            base = 20 if (focused and on_tab) else 45     # events do the real work
        elif focused and on_tab:
            base = 3
        elif focused:
            base = 10
        else:
            base = 15 if kind == "chat" else 20
        if kind == "rooms" and len(self.room_rows) > 20 and not self._events_live():
            base *= 2                    # many rooms and no events feed: go easy
        sec = min(60, base * 2 ** min(fails[kind], 3))
        setattr(self, f"{kind}_timer", GLib.timeout_add_seconds(sec, self._poll_cb, kind))

    def _poll_cb(self, kind):
        setattr(self, f"{kind}_timer", 0)
        (self._poll_chat if kind == "chat" else self._poll_rooms)()
        return False

    def _poll_chat(self, *_):
        if not self.api:
            self.chat_timer = 0
            return False
        if self.chat_timer:
            GLib.source_remove(self.chat_timer)
            self.chat_timer = 0
        if self.chat_busy:
            return False
        self.chat_busy = True
        api, user = self.api, self.chat_user

        def work():
            return api.conversations(), (api.messages(user) if user else None)

        def ok(res):
            self.chat_busy = False
            if api is self.api:
                self._update_chat(res[0], user, res[1])
            self._next_poll("chat")

        def err(_e):
            self.chat_busy = False
            self._next_poll("chat", ok=False)

        run_async(work, ok, err)
        return False

    def _update_chat(self, convs, user, msgs):
        api = self.api
        viewing_tab = (self.viewstack.get_visible_child_name() == "chat"
                       and self._window_focused())
        seeding = not self.chat_seeded
        total = 0
        for c in convs:
            name = c.get("username")
            if not name:
                continue
            unread = c.get("unAcknowledgedMessageCount") or \
                (1 if c.get("hasUnAcknowledgedMessages") else 0)
            if name not in self.chat_rows:
                self._add_chat_row(name)
            raw_unread = unread
            if viewing_tab and name == self.chat_user and unread:
                run_async(lambda n=name: api.ack_conversation(n))
                self._withdraw(f"chat-{name}")
                unread = 0
            _row, badge = self.chat_rows[name]
            badge.set_text(str(unread))
            badge.set_visible(unread > 0)
            total += unread
            prev = self.chat_unread.get(name, 0)
            self.chat_unread[name] = raw_unread
            if not seeding and raw_unread > prev:
                self._notify_chat(name)
        self.chat_seeded = True
        self._set_chat_badge(total)
        if user and user == self.chat_user and msgs is not None:
            self._render_messages(msgs)

    def _add_chat_row(self, name):
        row, badge = make_sidebar_row(
            name, "digger-user-symbolic",
            lambda: self._on_close_chat(name), "Close conversation",
            dot_dir=self.user_dir)
        row._name = name
        self.chat_list.append(row)
        self.chat_rows[name] = (row, badge)
        return row

    def _on_new_chat(self, entry):
        name = entry.get_text().strip()
        if not name:
            return
        entry.set_text("")
        self.new_chat_pop.popdown()
        row = self.chat_rows[name][0] if name in self.chat_rows \
            else self._add_chat_row(name)
        self.chat_list.select_row(row)
        self.msg_entry.grab_focus()
        self._hide_sidebar_if_collapsed()

    def _on_chat_selected(self, _lb, row):
        if row is None:
            return
        self.chat_user = row._name
        self.chat_stack.set_visible_child_name("chat")
        self.chat_header.set_user(row._name)
        self.msg_entry.set_sensitive(True)
        self._clear_messages()
        self.chat_sig = None
        self._fetch_messages(row._name)
        self._withdraw(f"chat-{row._name}")
        self._poll_chat()
        self._hide_sidebar_if_collapsed()

    def _fetch_messages(self, user):
        api = self.api

        def ok(msgs):
            if user == self.chat_user and api is self.api:
                self._render_messages(msgs)

        run_async(lambda: api.messages(user), ok,
                  lambda e: self.toast(describe_error(e)))

    def _clear_messages(self):
        child = self.msg_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.msg_list.remove(child)
            child = nxt

    def _reset_chat_view(self):
        self.chat_user = None
        self.chat_sig = None
        self._clear_messages()
        self.chat_stack.set_visible_child_name("empty")
        self.chat_header.set_user(None)
        self.msg_entry.set_sensitive(False)

    @staticmethod
    def _fmt_time(ts):
        try:
            dt = GLib.DateTime.new_from_iso8601(ts, None)
            return dt.to_local().format("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            return ts or ""

    def _render_messages(self, msgs):
        msgs = sorted(msgs, key=lambda m: m.get("timestamp", ""))
        sig = (self.chat_user, len(msgs), msgs[-1].get("id") if msgs else None)
        if sig == self.chat_sig:
            return
        self.chat_sig = sig
        render_bubbles(self.msg_list, [
            (m.get("username", ""), m.get("timestamp"), m.get("message", ""),
             m.get("direction") == "Out") for m in msgs], show_author=True)

        def scroll():
            adj = self.msg_scroll.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False

        GLib.timeout_add(60, scroll)

    def _on_send(self, *_):
        text = self.msg_entry.get_text().strip()
        user, api = self.chat_user, self.api
        if not text or not user or not api:
            return
        self.msg_entry.set_text("")

        def ok(_):
            self._fetch_messages(user)
            self._poll_chat()

        run_async(lambda: api.send_message(user, text), ok,
                  lambda e: self.toast(describe_error(e)))

    def _on_close_chat(self, name):
        api = self.api

        def ok(_):
            entry = self.chat_rows.pop(name, None)
            if entry:
                self.chat_list.remove(entry[0])
            if self.chat_user == name:
                self._reset_chat_view()

        run_async(lambda: api.close_conversation(name), ok,
                  lambda e: self.toast(describe_error(e)))
