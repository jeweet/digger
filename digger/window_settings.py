"""Main window: menu, settings dialogs and server settings."""

import json
import os
import platform
import re
import urllib.error
import urllib.parse
import urllib.request

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from . import constants
from .constants import LOG_FILE, LOG_LEVELS, PAGE_SHORTCUTS, SORT_MODES, THEMES, VERSION
from .logger import log
from .compat import Secret
from .util import describe_error
from .config import apply_theme, load_password, setup_logging
from .asyncutil import run_async
from .widgets import clear_children, sidebar_placeholder


class SettingsMixin:
    # ---- menu / settings / about --------------------------------------------
    def _on_conn_state(self, known, connected):
        act = self.lookup_action("server-toggle")
        if act:
            act.set_enabled(known)
        if known:
            self.server_connected = connected
        self._sync_conn_buttons(known)

    def _server_label(self):
        return "Disconnect" if self.server_connected else "Connect"

    def _sync_conn_buttons(self, sensitive):
        for b in list(self.conn_buttons):
            b.set_label(self._server_label())
            b.set_sensitive(sensitive)

    def _toggle_server(self, *_):
        api = self.api
        if not api:
            return
        want = not self.server_connected
        act = self.lookup_action("server-toggle")
        act.set_enabled(False)
        self._sync_conn_buttons(False)
        log.info("%s Soulseek server", "Connecting to" if want else "Disconnecting from")

        def finish(_r=None):
            self.status_bar._poll()          # re-enables the action via the new state

        def err(e):
            self.toast(describe_error(e))
            act.set_enabled(True)
            self._sync_conn_buttons(True)
            self.status_bar._poll()

        run_async(api.server_connect if want else api.server_disconnect, finish, err)

    def _build_menu_button(self, signout):
        menu = Gio.Menu()
        menu.append("Settings", "win.settings")
        menu.append("Server Settings", "win.server-settings")
        section = Gio.Menu()
        section.append("Keyboard Shortcuts", "win.shortcuts")
        section.append("About Digger", "win.about")
        section.append("Quit", "win.quit")
        menu.append_section(None, section)
        return Gtk.MenuButton(icon_name="digger-more-symbolic", menu_model=menu,
                              tooltip_text="Main menu", primary=signout)

    def _shortcut_new_search(self, *_):
        self.viewstack.set_visible_child_name("search")
        if self.split.get_collapsed():
            self.split.set_show_sidebar(True)
        self.search_pop.popup()

    def _shortcut_filter(self, *_):
        self.viewstack.set_visible_child_name("search")
        if self.filter_row.get_visible():
            self.filter_entry.grab_focus()

    def _show_shortcuts(self, *_):
        groups = [
            ("General", [("Keyboard shortcuts", "<Control>question"),
                         ("Settings", "<Control>comma"),
                         ("Quit", "<Control>q")]),
            ("Navigation", [(title, "<Alt>%d" % i) for i, (_k, title)
                            in enumerate(PAGE_SHORTCUTS.items(), 1)]),
            ("Search", [("New search", "<Control>n"),
                        ("Filter the results", "<Control>f")]),
        ]
        if hasattr(Adw, "ShortcutsDialog"):
            dlg = Adw.ShortcutsDialog()
            for title, items in groups:
                section = Adw.ShortcutsSection(title=title)
                for label, accel in items:
                    section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
                dlg.add(section)
            dlg.present(self)
            return
        try:                                    # older libadwaita
            win = Gtk.ShortcutsWindow(transient_for=self, modal=True)
            section = Gtk.ShortcutsSection(section_name="shortcuts", visible=True,
                                           max_height=12)
            for title, items in groups:
                group = Gtk.ShortcutsGroup(title=title, visible=True)
                for label, accel in items:
                    group.append(Gtk.ShortcutsShortcut(title=label, accelerator=accel,
                                                       visible=True))
                section.append(group)
            win.add_section(section)
            win.present()
        except Exception as e:  # noqa: BLE001
            log.debug("shortcuts window unavailable: %s", e)
            self.toast("Alt+1…6 pages · Ctrl+N new search · Ctrl+F filter · "
                       "Ctrl+, settings · Ctrl+Q quit")

    def _debug_info(self):
        """Plain-text troubleshooting report for the About dialog (no secrets)."""
        def ver(mod, *names):
            try:
                return ".".join(str(getattr(mod, n)()) for n in names)
            except Exception:  # noqa: BLE001
                return "?"
        lines = [
            f"Digger {VERSION} ({constants.APP_ID})",
            f"Python {platform.python_version()} on {platform.system()} {platform.release()} "
            f"({platform.machine()})",
            f"GTK {ver(Gtk, 'get_major_version', 'get_minor_version', 'get_micro_version')}, "
            f"libadwaita {ver(Adw, 'get_major_version', 'get_minor_version', 'get_micro_version')}, "
            f"GLib {GLib.MAJOR_VERSION}.{GLib.MINOR_VERSION}.{GLib.MICRO_VERSION}",
            "Flatpak: " + ("yes" if os.path.exists("/.flatpak-info") else "no"),
            "Session: " + (os.environ.get("XDG_SESSION_TYPE") or "?")
            + " / " + (os.environ.get("XDG_CURRENT_DESKTOP") or "?"),
            "Keyring (libsecret): " + ("available" if Secret else "not available"),
            "Password stored in: " + ("config file" if self.cfg.get("password") else "keyring/none"),
            "Adw.AboutDialog: " + ("yes" if hasattr(Adw, "AboutDialog") else "no (fallback)"),
        ]
        url = self.cfg.get("url") or ""
        m = re.match(r"(https?)://[^/:]*(?::(\d+))?", url)
        lines.append("Server: " + (f"{m.group(1)}, port {m.group(2) or 'default'} (host hidden)"
                                    if m else "not set"))
        lines.append("Connected: " + ("yes" if self.api else "no"))
        lines.append("Log level: " + str(self.cfg.get("log_level", "INFO")))
        lines.append("Config: " + constants.CONFIG_PATH)
        lines.append("Log file: " + LOG_FILE)
        tail = ""
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                tail = "".join(f.readlines()[-60:]).rstrip()
        except OSError:
            pass
        lines.append("")
        lines.append("Last log lines:" if tail else "No log file yet.")
        if tail:
            lines.append(tail)
        lines.append("")
        lines.append("Review this text before posting it publicly: log lines can "
                     "contain user names and file names.")
        return "\n".join(lines)

    def _show_about(self, *_):
        kw = dict(application_name="Digger", application_icon=constants.ICON_NAME,
                  developer_name="Digger contributors", version=VERSION,
                  comments="An unofficial GTK 4 / libadwaita client for the "
                           "slskd REST API.",
                  website="https://github.com/slskd/slskd",
                  copyright="© 2026 Digger contributors",
                  license_type=Gtk.License.MIT_X11)
        kw["issue_url"] = "https://github.com/jeweet/digger/issues"
        kw["debug_info"] = self._debug_info()
        kw["debug_info_filename"] = "digger-debug-info.txt"
        if hasattr(Adw, "AboutDialog"):
            Adw.AboutDialog(**kw).present(self)
        else:
            Adw.AboutWindow(transient_for=self, modal=True, **kw).present()

    def _present_prefs(self, page):
        if hasattr(Adw, "PreferencesDialog"):
            dlg = Adw.PreferencesDialog(title="Settings")
            dlg.add(page)
            dlg.set_search_enabled(False)
            dlg.present(self)
        else:
            dlg = Adw.PreferencesWindow(title="Settings", transient_for=self,
                                        modal=True, search_enabled=False)
            dlg.add(page)
            dlg.present()
        return dlg

    @staticmethod
    def _http_body(e):
        """Best-effort error text from an HTTPError (the server explains 400s)."""
        try:
            body = e.read().decode("utf-8", "replace").strip()
            try:
                parsed = json.loads(body)
                if isinstance(parsed, str):
                    body = parsed
            except ValueError:
                pass
            return body or describe_error(e)
        except Exception:  # noqa: BLE001
            return describe_error(e)

    def _confirm(self, heading, body, label, on_yes, destructive=True):
        """Small 'are you sure' dialog; on_yes runs only after confirming."""
        if hasattr(Adw, "AlertDialog"):
            dlg = Adw.AlertDialog(heading=heading, body=body)
        else:
            dlg = Adw.MessageDialog(transient_for=self, heading=heading, body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("yes", label)
        dlg.set_response_appearance(
            "yes", Adw.ResponseAppearance.DESTRUCTIVE if destructive
            else Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: on_yes() if r == "yes" else None)
        if hasattr(Adw, "AlertDialog"):
            dlg.present(self)
        else:
            dlg.present()

    def _build_server_page(self, api):
        """Version info and maintenance actions for the slskd server."""
        page = Adw.PreferencesPage()

        info = Adw.PreferencesGroup(title="Version")
        ver_row = Adw.ActionRow(title="slskd", subtitle="Loading…")
        check = Gtk.Button(label="Check for Updates", valign=Gtk.Align.CENTER)
        ver_row.add_suffix(check)
        info.add(ver_row)
        page.add(info)

        def show_version(app):
            v = (app or {}).get("version") or {}
            text = f"Running {v.get('current', '?')}"
            if v.get("latest"):
                text += f" · latest {v['latest']}"
            if v.get("isUpdateAvailable"):
                text += " · update available"
            ver_row.set_subtitle(text)

        def load_version():
            run_async(api.application, show_version,
                      lambda e: ver_row.set_subtitle(describe_error(e)))

        def on_check(btn):
            btn.set_sensitive(False)
            ver_row.set_subtitle("Checking…")

            def done(_r=None):
                btn.set_sensitive(True)
                load_version()

            def err(e):
                btn.set_sensitive(True)
                ver_row.set_subtitle(describe_error(e))

            run_async(api.check_latest_version, done, err)

        check.connect("clicked", on_check)
        load_version()

        net = Adw.PreferencesGroup(title="Soulseek Network")
        conn_row = Adw.ActionRow(
            title="Server connection",
            subtitle="Connect or disconnect slskd from the Soulseek network.")
        conn_btn = Gtk.Button(label=self._server_label(), valign=Gtk.Align.CENTER,
                              sensitive=self.lookup_action("server-toggle").get_enabled())
        conn_btn.connect("clicked", lambda *_: self._toggle_server())
        conn_row.add_suffix(conn_btn)
        net.add(conn_row)
        page.add(net)
        self.conn_buttons.add(conn_btn)

        shares = Adw.PreferencesGroup(title="Shared files")
        scan_row = Adw.ActionRow(title="Rescan shares",
                                 subtitle="Re-index the folders slskd shares")
        scan_btn = Gtk.Button(label="Rescan", valign=Gtk.Align.CENTER)
        scan_bar = Gtk.ProgressBar(visible=False, valign=Gtk.Align.CENTER,
                                   width_request=90)
        scan_row.add_suffix(scan_bar)          # left of the button, same row
        scan_row.add_suffix(scan_btn)
        shares.add(scan_row)
        page.add(shares)
        scan = {"scanning": False, "timer": 0}

        def show_scan(app):
            sh = (app or {}).get("shares") or {}
            scanning = bool(sh.get("scanning"))
            scan["scanning"] = scanning
            scan_btn.set_label("Cancel Scan" if scanning else "Rescan")
            files, dirs = sh.get("files"), sh.get("directories")
            if scanning:
                prog = sh.get("progress")
                if isinstance(prog, (int, float)):
                    frac = prog if prog <= 1 else prog / 100
                    scan_bar.set_fraction(max(0.0, min(1.0, frac)))
                    scan_row.set_subtitle(f"Scanning… {frac * 100:.0f}%")
                else:
                    scan_bar.pulse()
                    scan_row.set_subtitle("Scanning…")
            elif sh.get("faulted"):
                scan_row.set_subtitle("The last scan failed")
            elif isinstance(files, int):
                scan_row.set_subtitle(
                    f"{files:,} files in {dirs:,} folders" if isinstance(dirs, int)
                    else f"{files:,} files shared")
            scan_bar.set_visible(scanning)

        def poll_scan(*_):
            # keeps updating while the dialog is open and a scan is running
            if scan_row.get_root() is None:
                scan["timer"] = 0
                return False

            def ok(app):
                show_scan(app)
                if scan["scanning"] and scan_row.get_root() is not None:
                    scan["timer"] = GLib.timeout_add(1500, poll_scan)
                else:
                    scan["timer"] = 0
                    self.status_bar._poll()
            run_async(api.application, ok, lambda e: scan.update(timer=0))
            return False

        def start_polling():
            if not scan["timer"]:
                scan["timer"] = GLib.timeout_add(300, poll_scan)

        def on_scan(btn):
            cancel = scan["scanning"]
            btn.set_sensitive(False)

            def ok(_r=None):
                btn.set_sensitive(True)
                scan_row.set_subtitle("Cancelled" if cancel else "Scan started…")
                start_polling()

            def err(e):
                btn.set_sensitive(True)
                if isinstance(e, urllib.error.HTTPError) and e.code == 409:
                    self.toast("A share scan is already running")
                    start_polling()
                else:
                    self.toast(describe_error(e))

            run_async(api.cancel_share_scan if cancel else api.rescan_shares, ok, err)

        scan_btn.connect("clicked", on_scan)
        start_polling()

        actions = Adw.PreferencesGroup(title="Maintenance")

        def action_row(title, subtitle, label, callback, destructive=False):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            btn = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            if destructive:
                btn.add_css_class("destructive-action")
            btn.connect("clicked", callback)
            row.add_suffix(btn)
            actions.add(row)

        def run(fn, ok_text):
            run_async(fn, lambda _r=None: self.toast(ok_text),
                      lambda e: self.toast(
                          "Needs an administrator account" if isinstance(
                              e, urllib.error.HTTPError) and e.code in (401, 403)
                          else describe_error(e)))

        action_row("Free memory", "Run a garbage collection on the server.", "Run",
                   lambda *_: run(api.collect_garbage, "Garbage collection started"))
        action_row("Restart slskd",
                   "Restarts the server process. The client reconnects by itself "
                   "once it is back.", "Restart…",
                   lambda *_: self._confirm(
                       "Restart slskd?", "Active transfers are interrupted.", "Restart",
                       lambda: run(api.restart_server, "Restarting slskd…")))
        action_row("Shut down slskd",
                   "Stops the server. You will have to start it again yourself.",
                   "Shut Down…",
                   lambda *_: self._confirm(
                       "Shut down slskd?",
                       "The server stops and this client can no longer reach it "
                       "until you start it again.", "Shut Down",
                       lambda: run(api.shutdown_server, "Shutting down slskd…")),
                   destructive=True)
        page.add(actions)
        return page

    def _build_files_page(self, api):
        """Browse and delete finished / incomplete downloads on the server."""
        state = {"area": "downloads", "path": ""}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        area = Gtk.DropDown.new_from_strings(["Downloads", "Incomplete"])
        up = Gtk.Button(icon_name="digger-up-symbolic", tooltip_text="Parent folder")
        refresh = Gtk.Button(icon_name="digger-refresh-symbolic", tooltip_text="Reload")
        where = Gtk.Label(xalign=0, hexpand=True, ellipsize=1)
        where.add_css_class("dim-label")
        top = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=12,
                      margin_end=12)
        for w in (area, up, where, refresh):
            top.append(w)
        box.append(top)
        box.append(Gtk.Separator())

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("navigation-sidebar")
        listbox.set_placeholder(sidebar_placeholder("Empty folder"))
        box.append(Gtk.ScrolledWindow(child=listbox, vexpand=True))
        box.append(Gtk.Separator())
        status = Gtk.Label(xalign=0, wrap=True, margin_top=8, margin_bottom=8,
                           margin_start=12, margin_end=12)
        status.add_css_class("dim-label")
        box.append(status)

        def say(msg, error=False):
            status.set_text(msg)
            if error:
                status.remove_css_class("dim-label")
                status.add_css_class("error")
            else:
                status.remove_css_class("error")
                status.add_css_class("dim-label")

        def err_text(e):
            if isinstance(e, urllib.error.HTTPError):
                if e.code == 403:
                    return ("Not allowed. Deleting needs remote_file_management: true "
                            "on the server.")
                if e.code == 404:
                    return "Not found on the server"
            return describe_error(e)

        def load(*_):
            say("Loading…")
            where.set_text("/" + state["path"])
            up.set_sensitive(bool(state["path"]))
            area_name, rel = state["area"], state["path"]

            def ok(res):
                if (area_name, rel) != (state["area"], state["path"]):
                    return
                clear_children(listbox)
                dirs = sorted(res.get("directories") or [],
                              key=lambda d: (d.get("name") or d.get("fullName") or "").lower())
                files = sorted(res.get("files") or [],
                               key=lambda d: (d.get("name") or d.get("fullName") or "").lower())
                for entry, is_dir in [(d, True) for d in dirs] + [(f, False) for f in files]:
                    add_row(entry, is_dir)
                say(f"{len(dirs)} folder{'s' if len(dirs) != 1 else ''}, "
                    f"{len(files)} file{'s' if len(files) != 1 else ''}")

            run_async(lambda: api.files_list(area_name, rel), ok,
                      lambda e: (clear_children(listbox), say(err_text(e), True)))

        def entry_name(entry):
            name = entry.get("name") or entry.get("fullName") or "?"
            return re.split(r"[\\/]", name.rstrip("\\/"))[-1] or name

        def add_row(entry, is_dir):
            name = entry_name(entry)
            rel = f"{state['path']}/{name}" if state["path"] else name
            size = entry.get("length") if entry.get("length") is not None \
                else entry.get("size")
            subtitle = "" if is_dir or size is None else GLib.format_size(int(size))
            row = Adw.ActionRow(title=name, subtitle=subtitle, use_markup=False,
                                activatable=is_dir)
            row.add_prefix(Gtk.Image(icon_name="digger-folder-symbolic" if is_dir
                                     else "digger-file-symbolic"))
            trash = Gtk.Button(icon_name="digger-trash-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text="Delete")
            trash.add_css_class("flat")
            trash.connect("clicked", lambda *_: confirm_delete(name, rel, is_dir))
            row.add_suffix(trash)
            if is_dir:
                row.add_suffix(Gtk.Image(icon_name="digger-next-symbolic"))
                row.connect("activated", lambda *_: enter(rel))
            listbox.append(row)

        def enter(rel):
            state["path"] = rel
            load()

        def go_up(*_):
            state["path"] = state["path"].rsplit("/", 1)[0] if "/" in state["path"] else ""
            load()

        def on_area(dd, _p):
            state["area"] = "downloads" if dd.get_selected() == 0 else "incomplete"
            state["path"] = ""
            load()

        def confirm_delete(name, rel, is_dir):
            body = ("This permanently deletes the folder and everything in it from the "
                    "server." if is_dir else "This permanently deletes the file from "
                    "the server.")

            def do():
                say("Deleting…")
                area_name = state["area"]

                def done(_r=None):
                    say(f"Deleted {name}")
                    load()

                run_async(lambda: api.files_delete(area_name, rel, is_dir), done,
                          lambda e: say(err_text(e), True))

            self._confirm(f"Delete “{name}”?", body, "Delete", do)

        up.connect("clicked", go_up)
        refresh.connect("clicked", load)
        area.connect("notify::selected", on_area)
        load()
        return box

    def _build_logs_page(self, api):
        """Recent server log lines, or the tail of one of the log files."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        source = Gtk.DropDown.new_from_strings(["Live (recent)"])
        refresh = Gtk.Button(icon_name="digger-refresh-symbolic", tooltip_text="Reload")
        note = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        note.add_css_class("dim-label")
        top = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=12,
                      margin_end=12)
        for w in (source, note, refresh):
            top.append(w)
        box.append(top)
        box.append(Gtk.Separator())
        # wrap long lines so only a vertical scroll is needed
        view = Gtk.TextView(monospace=True, editable=False, cursor_visible=False,
                            top_margin=8, bottom_margin=8, left_margin=10,
                            right_margin=10, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.add_css_class("log-view")
        box.append(Gtk.ScrolledWindow(
            child=view, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        names = []

        def show(text):
            buf = view.get_buffer()
            buf.set_text(text)

            def to_end():
                it = buf.get_end_iter()
                view.scroll_to_iter(it, 0.0, False, 0.0, 0.0)
                return False

            GLib.idle_add(to_end)

        def fmt_entry(e):
            if not isinstance(e, dict):
                return str(e)
            ts = e.get("timestamp") or e.get("Timestamp") or e.get("@t") or ""
            lvl = e.get("level") or e.get("Level") or e.get("@l") or ""
            msg = (e.get("message") or e.get("Message") or e.get("@m")
                   or e.get("renderedMessage") or "")
            return " ".join(x for x in (str(ts), str(lvl), str(msg)) if x) \
                or json.dumps(e)

        def err_text(e):
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
                return "Logs are only available to administrators."
            return describe_error(e)

        def load(*_):
            idx = source.get_selected()
            note.set_text("Loading…")
            if idx == 0:
                def ok(res):
                    lines = [fmt_entry(x) for x in (res if isinstance(res, list) else [])]
                    show("\n".join(lines) or "(no log lines)")
                    note.set_text(f"{len(lines)} recent lines")
                run_async(api.logs_live, ok, lambda e: (show(err_text(e)),
                                                        note.set_text("")))
            else:
                name = names[idx - 1]

                def ok_file(txt):
                    show(txt or "(empty)")
                    note.set_text(f"Last 200 KB of {name}")
                run_async(lambda: api.log_file_tail(name), ok_file,
                          lambda e: (show(err_text(e)), note.set_text("")))

        def fill_files(res):
            items = []
            for x in res if isinstance(res, list) else []:
                n = x.get("name") if isinstance(x, dict) else x
                if isinstance(n, str):
                    items.append(n)
            items.sort(reverse=True)           # newest dated file first
            names.extend(items)
            source.set_model(Gtk.StringList.new(["Live (recent)"] + items))
            source.set_selected(0)
            source.connect("notify::selected", load)

        refresh.connect("clicked", load)
        run_async(api.logs_files, fill_files,
                  lambda e: source.connect("notify::selected", load))
        load()
        return box

    def _open_server_settings(self, *_):
        api = self.api
        if not api:
            self.toast("Sign in first")
            return
        mono = Gtk.TextView(monospace=True, top_margin=8, bottom_margin=8,
                            left_margin=10, right_margin=10)

        # --- tab 1: current options (read-only, secrets are redacted by slskd) ---
        current = Gtk.TextView(monospace=True, editable=False, cursor_visible=False,
                               top_margin=8, bottom_margin=8, left_margin=10,
                               right_margin=10)
        cur_scroll = Gtk.ScrolledWindow(child=current, vexpand=True)

        def load_current():
            def ok(res):
                current.get_buffer().set_text(json.dumps(res, indent=2)
                                              if res is not None else "(empty)")
            run_async(api.options, ok, lambda e: current.get_buffer().set_text(
                describe_error(e)))

        # --- tab 2: YAML editor ---
        editor = mono
        ed_scroll = Gtk.ScrolledWindow(child=editor, vexpand=True)
        status = Gtk.Label(xalign=0, wrap=True, hexpand=True, margin_start=12,
                           margin_end=12)
        status.add_css_class("dim-label")
        validate = Gtk.Button(label="Validate")
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        reload_btn = Gtk.Button(icon_name="digger-refresh-symbolic",
                                tooltip_text="Reload from server")
        # status on its own line, buttons below: fits a phone screen
        status.set_margin_start(0)
        status.set_margin_end(0)
        status.set_hexpand(True)
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        for w in (reload_btn, validate, save):
            btns.append(w)
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8,
                      margin_bottom=8, margin_start=12, margin_end=12)
        bar.append(status)
        bar.append(btns)
        ed_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        ed_box.append(ed_scroll)
        ed_box.append(Gtk.Separator())
        ed_box.append(bar)

        locked = Adw.StatusPage(
            icon_name="digger-lock-symbolic", title="Remote configuration is off",
            description="To edit settings here, set remote_configuration: true in "
                        "slskd.yml (or SLSKD_REMOTE_CONFIGURATION=true), restart slskd, "
                        "and sign in as an administrator.")
        ed_stack = Gtk.Stack(vexpand=True)
        ed_stack.add_named(ed_box, "editor")
        ed_stack.add_named(locked, "locked")
        ed_stack.set_visible_child_name("editor")

        def text():
            b = editor.get_buffer()
            return b.get_text(b.get_start_iter(), b.get_end_iter(), True)

        def say(msg, error=False):
            status.set_text(msg)
            if error:
                status.remove_css_class("dim-label")
                status.add_css_class("error")
            else:
                status.remove_css_class("error")
                status.add_css_class("dim-label")

        def load_yaml(*_):
            say("Loading…")

            def ok(txt):
                editor.get_buffer().set_text(txt)
                say("Loaded from the server")

            def err(e):
                if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
                    ed_stack.set_visible_child_name("locked")
                else:
                    say(self._http_body(e) if isinstance(e, urllib.error.HTTPError)
                        else describe_error(e), True)

            run_async(api.options_yaml, ok, err)

        def do_validate(*_):
            say("Validating…")
            yaml_text = text()

            def ok(msg):
                say("Valid" if not msg else msg, bool(msg))

            run_async(lambda: api.validate_yaml(yaml_text), ok,
                      lambda e: say(self._http_body(e) if isinstance(
                          e, urllib.error.HTTPError) else describe_error(e), True))

        def do_save(*_):
            yaml_text = text()
            save.set_sensitive(False)
            say("Saving…")

            def work():
                msg = api.validate_yaml(yaml_text)
                if msg:
                    raise ValueError(msg)
                api.save_yaml(yaml_text)

            def ok(_r=None):
                save.set_sensitive(True)
                say("Saved. slskd reloads the file by itself; slot limits need a "
                    "restart and credential changes a reconnect.")
                load_current()

            def err(e):
                save.set_sensitive(True)
                say(self._http_body(e) if isinstance(e, urllib.error.HTTPError)
                    else str(e), True)

            run_async(work, ok, err)

        validate.connect("clicked", do_validate)
        save.connect("clicked", do_save)
        reload_btn.connect("clicked", load_yaml)

        stack = Adw.ViewStack()
        stack.add_titled_with_icon(cur_scroll, "current", "Current",
                                   "digger-eye-symbolic")
        stack.add_titled_with_icon(ed_stack, "yaml", "Edit YAML",
                                   "digger-edit-symbolic")
        stack.add_titled_with_icon(self._build_server_page(api), "server", "Server",
                                   "digger-gear-symbolic")
        stack.add_titled_with_icon(self._build_files_page(api), "files", "Files",
                                   "digger-folder-symbolic")
        stack.add_titled_with_icon(self._build_logs_page(api), "logs", "Logs",
                                   "digger-file-symbolic")
        # wide: tabs in the header; narrow (phone): title on top, tabs at the bottom,
        # like the Settings dialog. ViewSwitcherTitle decides by itself, no breakpoint.
        bottom = Adw.ViewSwitcherBar(stack=stack)
        if hasattr(Adw, "ViewSwitcherTitle"):
            title = Adw.ViewSwitcherTitle(stack=stack, title="Server Settings")
            title.bind_property("title-visible", bottom, "reveal",
                                GObject.BindingFlags.SYNC_CREATE)
        else:
            title = Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar(title_widget=title)
        view = Adw.ToolbarView(content=stack)
        view.add_top_bar(header)
        view.add_bottom_bar(bottom)
        root = view
        load_current()
        load_yaml()

        if hasattr(Adw, "Dialog"):
            dlg = Adw.Dialog(title="Server Settings", content_width=760,
                             content_height=620, child=root)
            dlg.present(self)
        else:
            dlg = Adw.Window(title="Server Settings", transient_for=self, modal=True,
                             default_width=760, default_height=620, content=root)
            dlg.present()

    def _open_settings(self, *_):
        page = Adw.PreferencesPage(title="Settings",
                                   icon_name="digger-gear-symbolic")

        # Appearance
        appearance = Adw.PreferencesGroup(title="Appearance")
        theme_keys = [k for k, _l in THEMES]
        theme_row = Adw.ComboRow(title="Theme",
                                 subtitle="Light, dark, or follow the system",
                                 model=Gtk.StringList.new([l for _k, l in THEMES]))
        cur = self.cfg.get("theme", "system")
        theme_row.set_selected(theme_keys.index(cur) if cur in theme_keys else 0)
        theme_row.connect("notify::selected", lambda r, _p:
                          self._set_theme(theme_keys[r.get_selected()]))
        appearance.add(theme_row)
        page.add(appearance)

        # Startup defaults
        startup = Adw.PreferencesGroup(title="Startup",
                                       description="What the client starts with")
        pages = [("last", "Last used page")] + list(PAGE_SHORTCUTS.items())
        page_row = Adw.ComboRow(title="Open on",
                                model=Gtk.StringList.new([l for _k, l in pages]))
        keys = [k for k, _l in pages]
        cur_page = self.cfg.get("start_page", "search")
        page_row.set_selected(keys.index(cur_page) if cur_page in keys else 1)
        page_row.connect("notify::selected", lambda r, _p:
                         self._set_option("start_page", keys[r.get_selected()]))
        startup.add(page_row)
        sort_keys = [k for k, _l in SORT_MODES]
        sort_row = Adw.ComboRow(title="Sort search results by",
                                model=Gtk.StringList.new([l for _k, l in SORT_MODES]))
        cur_sort = self.cfg.get("default_sort", "best")
        sort_row.set_selected(sort_keys.index(cur_sort) if cur_sort in sort_keys else 0)
        sort_row.connect("notify::selected", lambda r, _p:
                         self._set_option("default_sort", sort_keys[r.get_selected()]))
        startup.add(sort_row)
        free_row = Adw.SwitchRow(title="Free upload slots only",
                                 subtitle="Hide users without a free slot in search "
                                          "results",
                                 active=bool(self.cfg.get("default_free_only")))
        free_row.connect("notify::active", lambda r, _p:
                         self._set_option("default_free_only", r.get_active()))
        startup.add(free_row)
        page.add(startup)

        # Background
        background = Adw.PreferencesGroup(title="Background")
        bg_row = Adw.SwitchRow(
            title="Keep running in the background",
            subtitle="Closing the window keeps Digger running so transfers, "
                     "chat and notifications continue. Reopen it by starting the "
                     "app again; quit from the main menu (Ctrl+Q).",
            active=bool(self.cfg.get("run_in_background")))
        bg_row.connect("notify::active", lambda r, _p:
                       self._set_option("run_in_background", r.get_active()))
        background.add(bg_row)
        page.add(background)

        # Notifications
        notif = Adw.PreferencesGroup(
            title="Notifications",
            description="Desktop notifications are only shown while the window "
                        "is hidden or not focused.")
        chat_row = Adw.SwitchRow(title="Private messages",
                                 subtitle="A user sends you a message",
                                 active=bool(self.cfg.get("notify_chat")))
        chat_row.connect("notify::active", lambda r, _p:
                         self._set_option("notify_chat", r.get_active()))
        rooms_row = Adw.SwitchRow(title="Chat rooms",
                                  subtitle="New messages in rooms you have joined",
                                  active=bool(self.cfg.get("notify_rooms")))
        rooms_row.connect("notify::active", lambda r, _p:
                          self._set_option("notify_rooms", r.get_active()))
        mention_row = Adw.SwitchRow(
            title="Only when mentioned",
            subtitle="For rooms: only when your username appears in the message",
            active=bool(self.cfg.get("notify_rooms_mentions", True)))
        mention_row.connect("notify::active", lambda r, _p:
                            self._set_option("notify_rooms_mentions", r.get_active()))
        rooms_row.bind_property("active", mention_row, "sensitive",
                                GObject.BindingFlags.SYNC_CREATE)
        dl_row = Adw.SwitchRow(title="Finished downloads",
                               subtitle="A file or folder has finished downloading",
                               active=bool(self.cfg.get("notify_downloads")))
        dl_row.connect("notify::active", lambda r, _p:
                       self._set_option("notify_downloads", r.get_active()))
        test_btn = Gtk.Button(label="Send", valign=Gtk.Align.CENTER)
        test_btn.connect("clicked", lambda *_: self.notify(
            "test", "Digger", "Notifications are working."))
        test_row = Adw.ActionRow(title="Send a test notification")
        test_row.add_suffix(test_btn)
        test_row.set_activatable_widget(test_btn)
        for r in (chat_row, rooms_row, mention_row, dl_row, test_row):
            notif.add(r)
        page.add(notif)

        # Shares
        shares_group = Adw.PreferencesGroup(title="Shared folders")

        folders = Adw.ExpanderRow(title="Folders",
                                  subtitle="Loading…" if self.api else "Not signed in")
        shares_group.add(folders)
        if self.api:
            api = self.api

            def fill(res):
                items = []
                groups = res.values() if isinstance(res, dict) else [res]
                for lst in groups:
                    items.extend(x for x in (lst or []) if isinstance(x, dict))
                if not items:
                    folders.set_subtitle("No shared folders")
                    return
                folders.set_subtitle(f"{len(items)} shared folder"
                                     f"{'s' if len(items) != 1 else ''}")
                for sh in items:
                    title = sh.get("alias") or sh.get("remotePath") or sh.get("raw") or "?"
                    parts = [sh.get("localPath") or sh.get("raw") or ""]
                    dirs, files = sh.get("directories"), sh.get("files")
                    if isinstance(dirs, int) and isinstance(files, int):
                        parts.append(f"{dirs:,} folders · {files:,} files")
                    if sh.get("isExcluded"):
                        parts.append("excluded")
                    row = Adw.ActionRow(title=title, use_markup=False,
                                        subtitle="\n".join(x for x in parts if x),
                                        subtitle_selectable=True)
                    row.add_prefix(Gtk.Image(icon_name="digger-folder-symbolic"))
                    folders.add_row(row)

            run_async(api.shares, fill,
                      lambda e: folders.set_subtitle(describe_error(e)))
        page.add(shares_group)

        # Logging
        logging_group = Adw.PreferencesGroup(
            title="Logging",
            description=f"The client writes its log to stderr and {LOG_FILE}")
        level_keys = [k for _l, k in LOG_LEVELS]
        level_row = Adw.ComboRow(title="Log level",
                                 model=Gtk.StringList.new([l for l, _k in LOG_LEVELS]))
        cur = str(self.cfg.get("log_level", "INFO")).upper()
        level_row.set_selected(level_keys.index(cur) if cur in level_keys else 1)
        level_row.connect("notify::selected", lambda r, _p:
                          self._set_log_level(level_keys[r.get_selected()]))
        logging_group.add(level_row)
        page.add(logging_group)

        # Credentials
        store = ("the system keyring" if Secret else
                 "the config file (private permissions; no keyring available)")
        creds = Adw.PreferencesGroup(
            title="Credentials",
            description=f"The password is kept in {store} so you stay signed in.")
        url_row = Adw.EntryRow(title="Server URL")
        url_row.set_text(self.cfg.get("url", ""))
        user_row = Adw.EntryRow(title="Username")
        user_row.set_text(self.cfg.get("username", ""))
        pw_row = Adw.PasswordEntryRow(title="Password (leave empty to keep current)")
        stay = Adw.SwitchRow(title="Stay signed in",
                             subtitle="Sign in automatically when the app starts",
                             active=bool(self.cfg.get("stay_signed_in", True)))
        for r in (url_row, user_row, pw_row, stay):
            creds.add(r)
        actions = Gtk.Box(spacing=12, halign=Gtk.Align.END, margin_top=12)
        signout = Gtk.Button(label="Sign Out", sensitive=self.api is not None)
        signout.add_css_class("destructive-action")
        save = Gtk.Button(label="Save & Reconnect")
        save.add_css_class("suggested-action")
        actions.append(signout)
        actions.append(save)
        creds.add(actions)
        page.add(creds)

        dlg = self._present_prefs(page)

        def on_save(*_):
            url = url_row.get_text().strip()
            user = user_row.get_text().strip()
            typed = pw_row.get_text()
            if not url or not user:
                self.toast("Enter server URL and username")
                return

            def proceed(pw):
                if not pw:
                    self.toast("Enter a password")
                    return
                dlg.close()
                self._do_login(url, user, pw, remember=stay.get_active())

            if typed:
                proceed(typed)
            else:      # reuse the stored one; the keyring can be slow, so not on the UI thread
                run_async(lambda: load_password(self.cfg, url, user) or "", proceed,
                          lambda e: self.toast(describe_error(e)))

        def on_signout(*_):
            dlg.close()
            self._logout()

        save.connect("clicked", on_save)
        signout.connect("clicked", on_signout)

    def _set_option(self, key, value):
        self.cfg[key] = value
        log.info("Setting %s = %s", key, value)
        self._save_config()

    def _set_theme(self, key):
        self.cfg["theme"] = key
        apply_theme(key)
        self._save_config()

    def _set_log_level(self, key):
        self.cfg["log_level"] = key
        setup_logging(key)
        log.info("Log level set to %s", key)
        self._save_config()
