"""Main window: session, login and layout."""

import weakref

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from . import constants
from .constants import DEFAULT_CONFIG, PAGE_SHORTCUTS, VERSION
from .logger import log
from .util import describe_error
from .config import (
    apply_theme, forget_password, load_config, load_password, notification_icon,
    save_config, setup_logging, store_password,
)
from .icons import has_app_icon
from .api import Api
from .asyncutil import run_async
from .widgets import StatusDot, make_sidebar_row, make_spinner
from .messages import COPY_NOTIFY, set_messages_compact
from .models import SearchFilter
from .transfers import TransfersView
from .users import UserDirectory, UserHeader, UsersView
from .statusbar import StatusBar
from .window_settings import SettingsMixin
from .window_search import SearchMixin
from .window_chat import ChatMixin
from .window_events import EventsMixin
from .window_rooms import RoomsMixin


class MainWindow(SettingsMixin, SearchMixin, ChatMixin, EventsMixin, RoomsMixin, Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Digger", default_width=900,
                         default_height=700)
        self.cfg = load_config()
        setup_logging(self.cfg.get("log_level", "INFO"))
        apply_theme(self.cfg.get("theme", "system"))
        style = Adw.StyleManager.get_default()
        style.connect("notify::dark", self._sync_dark)
        self._sync_dark()
        log.info("Starting Digger %s", VERSION)

        self.api = None
        self.search_gen = 0
        self.chat_timer = 0
        self.chat_busy = False
        self.chat_rows = {}
        self.chat_user = None
        self.chat_sig = None
        self.me = ""
        self.rooms_timer = 0
        self.rooms_busy = False
        self.rooms_epoch = 0
        self.room_rows = {}
        self.room_cur = None
        self.room_sig = None
        self.users_sig = None
        self.avail_data = []
        self.res_files = 0
        self.res_dirs = 0
        self.recents = []
        self.searches = []         # searches known to the server (as in the web UI)
        self._search_sig = None
        self.raw, self.raw_files = [], 0       # unfiltered search responses
        self.raw_dirs = 0
        self.current_query = ""
        self.recent_stats = {}     # query (lowercase) -> [files, folders, users, time]
        self.search_time = 0
        self.search_id = ""
        self.search_cancelled = False
        self.recent_rows = {}
        self.searching = False
        self.search_started = False
        self.flt = SearchFilter()
        self.sort_mode = "best"
        self.dl_index = {}           # (user, filename) -> download state
        self._result_widgets = weakref.WeakSet()
        self.dot_timer = 0
        self.chat_unread = {}        # conversation -> unread count at last poll
        self.chat_seeded = False     # first poll only records state
        self.chat_notified = set()   # message ids already notified
        self.room_seen = {}          # room -> newest timestamp already seen
        self.room_unread = {}        # room -> messages received since you last looked
        self.ev_timer, self.ev_busy, self.ev_ok, self.ev_last_ok = 0, False, False, 0
        self.ev_seen, self.ev_first, self.ev_fails = set(), True, 0
        self.room_dirty, self.room_dirty_unknown = set(), False
        self.user_dir = UserDirectory(lambda: self.api)

        self.server_connected = False
        self.conn_buttons = weakref.WeakSet()   # connect/disconnect buttons in Server Settings
        for name, cb in (("server-toggle", self._toggle_server),
                         ("settings", self._open_settings),
                         ("server-settings", self._open_server_settings),
                         ("about", self._show_about),
                         ("shortcuts", self._show_shortcuts),
                         ("new-search", self._shortcut_new_search),
                         ("focus-filter", self._shortcut_filter),
                         ("logout", lambda *_: self._logout()),
                         ("quit", lambda *_: self.get_application().quit())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        self.lookup_action("server-toggle").set_enabled(False)   # until slskd reports
        for page in PAGE_SHORTCUTS:
            action = Gio.SimpleAction.new("goto-" + page, None)
            action.connect("activate", lambda _a, _p, pg=page:
                           self.viewstack.set_visible_child_name(pg))
            self.add_action(action)

        self.connect("close-request", self._on_close_request)
        self.connect("notify::is-active", self._on_active_changed)

        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack()
        self.toasts.set_child(self.stack)
        self.set_content(self.toasts)

        self.stack.add_named(self._build_login(), "login")
        self.stack.add_named(self._build_splash(), "splash")
        self.stack.add_named(self._build_main(), "main")
        self.stack.set_visible_child_name("login")
        self._setup_breakpoint()

        self._apply_config()
        self._maybe_auto_login()

    def _sync_dark(self, *_):
        if Adw.StyleManager.get_default().get_dark():
            self.add_css_class("dark-ui")
        else:
            self.remove_css_class("dark-ui")

    def toast(self, text):
        toast = Adw.Toast(title=text)
        try:
            toast.set_use_markup(False)       # file names contain & and <
        except AttributeError:                # older libadwaita: escape instead
            toast.set_title(GLib.markup_escape_text(text))
        self.toasts.add_toast(toast)

    # ---- shared helpers ---------------------------------------------------
    def attach_user_tooltip(self, widget, getter):
        """Show user stats/details on hover. getter() -> (username, extra)|None."""
        widget.set_has_tooltip(True)

        def on_query(w, _x, _y, _kb, tip):
            got = getter()
            if not got:
                return False
            username, extra = got
            tip.set_markup(self.user_dir.markup(username, extra,
                                                w.trigger_tooltip_query))
            return True

        widget.connect("query-tooltip", on_query)

    def queue_download(self, username, payload):
        api = self.api
        n = len(payload)
        if not api or not n:
            return
        log.info("Queueing %d file(s) from %s", n, username)

        def work():
            for i in range(0, n, 500):
                api.download(username, payload[i:i + 500])

        run_async(work,
                  lambda _: self.toast(f"Queued {n} file{'s' if n != 1 else ''}"),
                  lambda e: self.toast(describe_error(e)))

    def browse_user(self, username):
        if not self.api:
            return
        self.viewstack.set_visible_child_name("users")
        self.users_view.open_user(username)
        self._hide_sidebar_if_collapsed()

    # ---- login ----------------------------------------------------------
    def _build_splash(self):
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.StatusPage(title="Signing in…")
        if has_app_icon():
            page.set_icon_name(constants.ICON_NAME)
        page.set_child(make_spinner(32))
        view.set_content(page)
        return view

    def _build_login(self):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.pack_end(self._build_menu_button(False))
        view.add_top_bar(header)

        clamp = Adw.Clamp(maximum_size=420, margin_top=24, margin_bottom=24,
                          margin_start=12, margin_end=12, valign=Gtk.Align.CENTER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(box)

        if has_app_icon():
            box.append(Gtk.Image(icon_name=constants.ICON_NAME, pixel_size=96))
        title = Gtk.Label(label="Connect to slskd")
        title.add_css_class("title-1")
        box.append(title)

        group = Adw.PreferencesGroup()
        self.url_row = Adw.EntryRow(title="Server URL")
        self.user_row = Adw.EntryRow(title="Username")
        self.pass_row = Adw.PasswordEntryRow(title="Password")
        self.stay_row = Adw.SwitchRow(title="Stay signed in",
                                      subtitle="Remember the credentials on this device",
                                      active=True)
        for r in (self.url_row, self.user_row, self.pass_row):
            group.add(r)
            r.connect("entry-activated", lambda *_: self._on_login())
        group.add(self.stay_row)
        box.append(group)

        self.login_btn = Gtk.Button(label="Sign In", halign=Gtk.Align.CENTER)
        self.login_btn.add_css_class("suggested-action")
        self.login_btn.add_css_class("pill")
        self.login_btn.connect("clicked", lambda *_: self._on_login())
        box.append(self.login_btn)

        view.set_content(clamp)
        return view

    def _apply_config(self):
        self.url_row.set_text(self.cfg.get("url") or DEFAULT_CONFIG["url"])
        self.user_row.set_text(self.cfg.get("username", ""))
        self.stay_row.set_active(bool(self.cfg.get("stay_signed_in", True)))
        self._refresh_recents()

    def _save_config(self):
        self.cfg.pop("recent_searches", None)     # history now lives on the server
        self.cfg.pop("recent_stats", None)
        save_config(self.cfg)

    def _maybe_auto_login(self):
        url, user = self.cfg.get("url"), self.cfg.get("username")
        if not (self.cfg.get("stay_signed_in") and url and user):
            return
        self.stack.set_visible_child_name("splash")

        def got(pw):
            if not pw:
                self.stack.set_visible_child_name("login")
                return
            log.info("Signing in automatically as %s", user)
            self._do_login(url, user, pw, remember=True, auto=True)

        def failed(e):
            log.warning("Could not read the stored password: %s", e)
            self.stack.set_visible_child_name("login")

        # the keyring can block (unlock prompt); never do that before the window shows
        run_async(lambda: load_password(self.cfg, url, user), got, failed)

    def _on_login(self):
        url = self.url_row.get_text().strip()
        user = self.user_row.get_text().strip()
        pw = self.pass_row.get_text()
        if not url or not user:
            self.toast("Enter server URL and username")
            return
        self._do_login(url, user, pw, remember=self.stay_row.get_active())

    def _do_login(self, url, user, pw, remember, auto=False):
        api = Api(url)
        self.login_btn.set_sensitive(False)
        log.info("Signing in to %s as %s", url, user)

        def done(_):
            self.login_btn.set_sensitive(True)
            if self.api:                      # reconnecting from Settings
                self._teardown_session()
            self.api = api
            self.cfg.update(url=url, username=user, stay_signed_in=remember)
            if not auto:
                if remember:
                    log.info("Credentials stored in %s",
                             store_password(self.cfg, url, user, pw))
                else:
                    forget_password(self.cfg, url, user)
            self._save_config()
            self.pass_row.set_text("")
            self.url_row.set_text(url)
            self.user_row.set_text(user)
            self._enter_main(user)

        def fail(e):
            self.login_btn.set_sensitive(True)
            log.warning("Sign-in failed: %s", describe_error(e))
            if not self.api:
                self.stack.set_visible_child_name("login")
            self.toast(describe_error(e))

        run_async(lambda: api.login(user, pw), done, fail)

    def my_names(self):
        """Your login name and your Soulseek name (they can differ), lowercase."""
        soulseek = ((self.status_bar.app.get("user") or {}).get("username") or "")
        return sorted({(self.me or "").lower(), soulseek.lower()} - {""})

    def is_self(self, username):
        name = (username or "").strip().lower()
        return bool(name) and name in self.my_names()

    def _enter_main(self, user):
        self.me = user
        self.stack.set_visible_child_name("main")
        self.status_bar.start()
        self.dot_timer = GLib.timeout_add_seconds(30, self._refresh_dots)
        self.downloads_view.start()
        self.uploads_view.start()
        self._start_chat_poll()
        self._start_rooms_poll()
        self._start_events()
        self._load_searches()

    def _teardown_session(self):
        self.search_gen += 1
        self.api = None
        self.results.remove_all()
        self.raw, self.raw_files, self.searching = [], 0, False
        self.raw_dirs = 0
        self.current_query = ""
        self.search_started = False
        self.search_cancelled = False
        self.searches, self._search_sig = [], None
        self._refresh_recents()
        self._update_results_page()
        self._select_current_recent()
        self.chat_unread.clear()
        self.chat_seeded = False
        self.chat_notified.clear()
        self.room_seen.clear()
        self.room_unread.clear()
        self._stop_events()
        self.downloads_view.stop()
        self.uploads_view.stop()
        self.users_view.stop()
        self.status_bar.stop()
        if self.dot_timer:
            GLib.source_remove(self.dot_timer)
            self.dot_timer = 0
        self.user_dir.reset()
        self._set_download_index({})
        if self.chat_timer:
            GLib.source_remove(self.chat_timer)
            self.chat_timer = 0
        for row, _badge in list(self.chat_rows.values()):
            self.chat_list.remove(row)
        self.chat_rows.clear()
        self._reset_chat_view()
        self._set_chat_badge(0)
        if self.rooms_timer:
            GLib.source_remove(self.rooms_timer)
            self.rooms_timer = 0
        self.rooms_epoch += 1
        for row in list(self.room_rows.values()):
            self.room_list.remove(row)
        self.room_rows.clear()
        self._update_room_badges()
        self._reset_room_view()

    def _refresh_dots(self):
        if not self.api:
            self.dot_timer = 0
            return False
        if self._window_focused():          # presence dots only matter when visible
            StatusDot.refresh_all()
        return True

    def shutdown(self):
        """Called when the application is quitting (window closed, Ctrl+C, SIGTERM)."""
        log.info("Shutting down")
        if self.dot_timer:
            GLib.source_remove(self.dot_timer)
            self.dot_timer = 0
        self._save_config()
        for attr in ("chat_timer", "rooms_timer"):
            if getattr(self, attr):
                GLib.source_remove(getattr(self, attr))
                setattr(self, attr, 0)
        self.downloads_view.stop()
        self.uploads_view.stop()
        self.status_bar.stop()
        self.api = None   # any in-flight poll results are discarded

    # ---- background running / notifications -----------------------------------
    def _on_close_request(self, *_):
        app = self.get_application()
        if self.cfg.get("run_in_background") and self.api and app is not None:
            log.info("Window closed; keeping the app running in the background")
            self.set_visible(False)
            app.enter_background()
            return True      # don't destroy the window
        return False

    def _on_active_changed(self, *_):
        self._poll_now()

    def _window_focused(self):
        return self.get_visible() and self.is_active()

    def notify(self, nid, title, body, action=None, target=None):
        app = self.get_application()
        if app is None:
            return
        n = Gio.Notification.new(title)
        n.set_body(body if len(body) <= 300 else body[:297] + "…")
        icon = notification_icon()
        if icon is not None:
            n.set_icon(icon)
        if action:
            # the detailed-name form works in every PyGObject version
            try:
                n.set_default_action(
                    Gio.Action.print_detailed_name(action, GLib.Variant("s", target)))
            except Exception as e:  # noqa: BLE001
                log.warning("Notification click action unavailable: %s", e)
        try:
            app.send_notification(nid, n)
            log.info("Notification: %s", title)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not send notification: %s", e)

    def _withdraw(self, nid):
        app = self.get_application()
        if app is not None:
            app.withdraw_notification(nid)

    def show_chat(self, username):
        if not self.api:
            return
        self.viewstack.set_visible_child_name("chat")
        entry = self.chat_rows.get(username)
        self.chat_list.select_row(entry[0] if entry else self._add_chat_row(username))

    def show_room(self, room):
        if not self.api:
            return
        self.viewstack.set_visible_child_name("rooms")
        row = self.room_rows.get(room)
        if row is not None:
            self.room_list.select_row(row)

    def _notify_chat(self, name):
        if not self.cfg.get("notify_chat") or self._window_focused():
            return
        api = self.api

        def ok(msgs):
            fresh = []
            for m in msgs:
                mid = m.get("id") or (m.get("timestamp"), m.get("message"))
                if m.get("direction") == "Out" or mid in self.chat_notified:
                    continue
                self.chat_notified.add(mid)
                fresh.append(m)
            if not fresh:
                return
            fresh.sort(key=lambda m: m.get("timestamp", ""))
            n = len(fresh)
            title = name if n == 1 else f"{name} ({n} new messages)"
            self.notify(f"chat-{name}", title, fresh[-1].get("message", ""),
                        "app.open-chat", name)

        run_async(lambda: api.messages(name, unacked_only=True), ok,
                  lambda e: log.debug("chat notification fetch failed: %s", e))

    def _update_room_badges(self):
        total = 0
        for name, row in self.room_rows.items():
            n = self.room_unread.get(name, 0)
            badge = getattr(row, "_badge", None)
            if badge is not None:
                badge.set_text(str(n))
                badge.set_visible(n > 0)
            total += n
        nav = self.nav_badges.get("rooms")
        if nav is not None:
            nav.set_text(str(total))
            nav.set_visible(total > 0)

    def _mark_room_read(self, room):
        if self.room_unread.pop(room, 0):
            self._update_room_badges()

    def _check_room_notifications(self, extra):
        """extra: {room: [messages]} fetched this poll. Counts unread messages per
        room (always) and sends notifications (when enabled)."""
        me = (self.me or "").lower()
        notify_on = bool(self.cfg.get("notify_rooms"))
        mentions_only = bool(self.cfg.get("notify_rooms_mentions"))
        looking = (self._window_focused()
                   and self.viewstack.get_visible_child_name() == "rooms")
        for room, msgs in (extra or {}).items():
            if not msgs:
                continue
            newest = max(m.get("timestamp", "") for m in msgs)
            last = self.room_seen.get(room)
            self.room_seen[room] = newest if last is None else max(last, newest)
            if last is None:                 # first sight: don't replay history
                continue
            fresh = sorted((m for m in msgs
                            if m.get("timestamp", "") > last
                            and (m.get("username") or "").lower() != me),
                           key=lambda m: m.get("timestamp", ""))
            if not fresh:
                continue
            if looking and room == self.room_cur:
                continue                     # you are reading it right now
            self.room_unread[room] = self.room_unread.get(room, 0) + len(fresh)
            if not notify_on or self._window_focused():
                continue
            if mentions_only and me:
                fresh = [m for m in fresh if me in (m.get("message") or "").lower()]
            if not fresh:
                continue
            n, tail = len(fresh), fresh[-1]
            if n == 1:
                title = f"{tail.get('username', '?')} in {room}"
                body = tail.get("message", "")
            else:
                title = f"{n} new messages in {room}"
                body = f"{tail.get('username', '?')}: {tail.get('message', '')}"
            self.notify(f"room-{room}", title, body, "app.open-room", room)
        if looking and self.room_cur:
            self.room_unread.pop(self.room_cur, None)
        self._update_room_badges()

    def _logout(self, forget=True):
        log.info("Signing out")
        if forget:
            forget_password(self.cfg, self.cfg.get("url", ""),
                            self.cfg.get("username", ""))
            self._save_config()
        self._teardown_session()
        self.stack.set_visible_child_name("login")

    # ---- main (tabs) ----------------------------------------------------
    def _build_main(self):
        self.viewstack = Adw.ViewStack()
        tab = lambda name: (lambda: self.viewstack.get_visible_child_name() == name)
        self.status_bar = StatusBar(lambda: self.api, lambda: self.me)
        self.status_bar.toast = self.toast
        self.status_bar.on_conn_state = self._on_conn_state
        self.status_bar.slow = lambda: not self._window_focused()
        self.downloads_view = TransfersView(
            "downloads", lambda: self.api, self.toast, tab("downloads"), host=self,
            on_stats=self.status_bar.set_transfer_stats,
            on_index=self._set_download_index)
        self.uploads_view = TransfersView(
            "uploads", lambda: self.api, self.toast, tab("uploads"), host=self,
            on_stats=self.status_bar.set_transfer_stats)
        self.users_view = UsersView(self)

        pages = [
            ("search", "Search", "digger-search-symbolic", self._build_search()),
            ("downloads", "Downloads", "digger-download-symbolic",
             self.downloads_view),
            ("uploads", "Uploads", "digger-upload-symbolic",
             self.uploads_view),
            ("users", "Users", "digger-user-symbolic", self.users_view),
            ("rooms", "Rooms", "digger-rooms-symbolic", self._build_rooms()),
            ("chat", "Chat", "digger-chat-symbolic", self._build_chat()),
        ]
        pages[-2], pages[-1] = pages[-1], pages[-2]   # menu order: Chat, then Rooms

        # sidebar, part 1: section navigation
        self.nav = Gtk.ListBox()
        self.nav.add_css_class("navigation-sidebar")
        self.nav_rows, self.nav_badges, self.nav_titles = {}, {}, {}
        for name, title, icon, widget in pages:
            self.viewstack.add_titled(widget, name, title)
            row, badge = make_sidebar_row(title, icon)
            row._page = name
            self.nav.append(row)
            self.nav_rows[name] = row
            self.nav_badges[name] = badge
            self.nav_titles[name] = title
        self.nav.connect("row-selected", self._on_nav_selected)
        self.nav.connect("row-activated", self._on_nav_activated)

        # sidebar, part 2: contextual list for the current section
        self.side_stack = Gtk.Stack(vexpand=True,
                                    transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.side_stack.add_named(self.side_search, "search")
        self.side_stack.add_named(self.users_view.sidebar, "users")
        self.side_stack.add_named(self.downloads_view.sidebar, "downloads")
        self.side_stack.add_named(self.uploads_view.sidebar, "uploads")
        self.side_stack.add_named(self.side_rooms, "rooms")
        self.side_stack.add_named(self.side_chat, "chat")
        self.side_stack.add_named(Gtk.Box(), "none")

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side.append(self.nav)
        self.side_sep = Gtk.Separator(margin_start=12, margin_end=12,
                                      margin_top=6)
        side.append(self.side_sep)
        side.append(self.side_stack)

        side_header = Adw.HeaderBar(show_end_title_buttons=False)
        side_header.set_title_widget(Adw.WindowTitle(title="Digger", subtitle=""))
        side_header.pack_end(self._build_menu_button(True))
        side_view = Adw.ToolbarView()
        side_view.add_top_bar(side_header)
        side_view.set_content(side)

        # content
        self.content_title = Adw.WindowTitle(title="Search", subtitle="")
        content_header = Adw.HeaderBar(show_start_title_buttons=False)
        content_header.set_title_widget(self.content_title)
        content_view = Adw.ToolbarView()
        content_view.add_top_bar(content_header)
        content_view.add_bottom_bar(self.status_bar)  # content pane only
        content_view.set_content(self.viewstack)

        self.viewstack.connect("notify::visible-child-name", self._on_page_changed)
        start = self.cfg.get("start_page", "search")
        if start == "last":
            start = self.cfg.get("last_page", "search")
        self.nav.select_row(self.nav_rows.get(start) or self.nav_rows["search"])
        self._on_page_changed()

        self.split = Adw.OverlaySplitView(
            sidebar=side_view, content=content_view,
            sidebar_width_fraction=0.25, min_sidebar_width=210,
            max_sidebar_width=300)

        # sidebar toggle: only visible while the sidebar is collapsed (phone)
        toggle = Gtk.ToggleButton(icon_name="digger-sidebar-symbolic",
                                  tooltip_text="Show sidebar")
        self.split.bind_property(
            "show-sidebar", toggle, "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self.split.bind_property("collapsed", toggle, "visible",
                                 GObject.BindingFlags.SYNC_CREATE)
        content_header.pack_start(toggle)
        content_header.pack_end(self.redo_btn)
        self.split.connect("notify::collapsed", self._on_collapsed)
        return self.split

    def _setup_breakpoint(self):
        """Phone layout: sidebar becomes an overlay opened by the header toggle."""
        self.set_size_request(360, 360)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600sp"))
        bp.add_setter(self.split, "collapsed", True)
        # partially cover the app (like Nautilus): leave a strip of content visible
        bp.add_setter(self.split, "sidebar-width-fraction", 0.7)
        bp.add_setter(self.split, "max-sidebar-width", 300.0)
        bp.add_setter(self.status_bar.user_box, "visible", False)
        bp.add_setter(self.sort_dd, "visible", False)
        bp.add_setter(self.free_check, "visible", False)
        bp.add_setter(self.opts_btn, "visible", True)
        bp.add_setter(self.status_bar.down_q, "visible", False)
        bp.add_setter(self.status_bar.up_q, "visible", False)
        bp.add_setter(self.status_bar.shares_box, "visible", False)
        bp.add_setter(self.help_btn, "visible", False)       # touch: dialog instead
        bp.add_setter(self.help_btn_touch, "visible", True)
        bp.connect("apply", lambda *_: (TransfersView.set_compact(True),
                                        UserHeader.set_compact(True),
                                        set_messages_compact(True)))
        bp.connect("unapply", lambda *_: (TransfersView.set_compact(False),
                                          UserHeader.set_compact(False),
                                          set_messages_compact(False)))
        COPY_NOTIFY[0] = self.toast
        self.add_breakpoint(bp)

    def _on_collapsed(self, split, _pspec):
        # collapsed: start with the content visible; wide: always show sidebar
        split.set_show_sidebar(not split.get_collapsed())

    def _hide_sidebar_if_collapsed(self):
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _on_nav_selected(self, _lb, row):
        if row is not None:
            self.viewstack.set_visible_child_name(row._page)

    def _on_nav_activated(self, _lb, row):
        self.viewstack.set_visible_child_name(row._page)
        # rooms/chat (and users, once some are open) need the contextual list
        keep = row._page in ("rooms", "chat") or \
            (row._page == "users" and bool(self.users_view.pages))
        if not keep:
            self._hide_sidebar_if_collapsed()

    def _on_page_changed(self, *_):
        name = self.viewstack.get_visible_child_name()
        row = self.nav_rows.get(name)
        if row is not None and self.nav.get_selected_row() is not row:
            self.nav.select_row(row)
        if name in PAGE_SHORTCUTS and self.cfg.get("last_page") != name:
            self.cfg["last_page"] = name          # for "start on the last used page"
            self._save_config()
        has_side = name in ("search", "users", "downloads", "uploads", "rooms",
                            "chat")
        self.side_stack.set_visible_child_name(name if has_side else "none")
        self.side_sep.set_visible(has_side)
        self.content_title.set_title(self.nav_titles.get(name, ""))
        self._sync_redo()
        self._poll_chat()
        self._poll_rooms()
        self.downloads_view.poll()
        self.uploads_view.poll()

    def _set_chat_badge(self, n):
        badge = self.nav_badges["chat"]
        badge.set_text(str(n))
        badge.set_visible(n > 0)
