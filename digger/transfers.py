"""Downloads / uploads view."""

import collections
import time
import weakref

from gi.repository import Adw, Gio, GLib, Gtk

from .constants import ICON_SIZE
from .logger import log
from .util import describe_error, tx_cat, tx_category
from .icons import set_item_icon
from .asyncutil import run_async
from .widgets import Placeholder, avatar_overlay, enable_row_toggle, make_sidebar_row
from .models import TxDir, TxFile, TxUser, cmp_tx


class TransfersView(Gtk.Box):
    """User -> folder -> file tree of slskd transfers (downloads or uploads)."""
    BAR_WIDE, BAR_COMPACT = 150, 24
    OP_ICONS = {"retry": "digger-refresh-symbolic", "cancel": "digger-stop-symbolic",
                "remove": "digger-trash-symbolic"}
    compact = False                     # phone layout: progress bars shrink a lot
    bars = weakref.WeakSet()
    views = weakref.WeakSet()

    @classmethod
    def set_compact(cls, compact):
        cls.compact = compact
        for bar in list(cls.bars):
            bar.set_size_request(cls.BAR_COMPACT if compact else cls.BAR_WIDE, -1)
        for view in list(cls.views):
            view._apply_op_labels()

    def __init__(self, kind, get_api, toast, is_visible, host=None, on_stats=None,
                 on_index=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.host = host
        self.on_stats = on_stats
        self.on_index = on_index
        self.pending = None           # data fetched while the tab was hidden
        self._prev = {}               # transfer id -> bytes at last sample
        self._last_t = None
        self._speed = None
        self.session_bytes = 0
        self.kind = kind
        TransfersView.views.add(self)
        self.get_api = get_api
        self.toast = toast
        self.is_visible = is_visible
        self.timer = 0
        self.busy = False
        self.fails = 0
        self.n_active = 0
        self.loaded = False
        self.users = {}
        self.last_data = None
        self.filter = "all"
        self.items = {}               # transfer id -> TxFile (for position updates)
        self.positions = {}           # transfer id -> (timestamp, position)
        self.pos_active = 0
        self.pos_pending = set()

        self.op_busy = False
        self.hidden_ids = set()       # removed by the user, the server is still catching up
        self.bar = self._build_actions()
        self.bar.set_visible(False)       # nothing to act on until transfers exist
        self.append(self.bar)
        # progress of long-running bulk operations (cancel / remove many transfers)
        self.prog_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.prog_label.add_css_class("dim-label")
        self.prog_bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=140)
        self.prog_box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6,
                                margin_start=12, margin_end=12, visible=False)
        self.prog_box.append(self.prog_label)
        self.prog_box.append(self.prog_bar)
        self.append(self.prog_box)
        self.removed_title = Gtk.Label(label=f"Removed {self.kind}", xalign=0,
                                       visible=False, margin_top=12, margin_bottom=6,
                                       margin_start=12, margin_end=12)
        self.removed_title.add_css_class("title-4")
        self.append(self.removed_title)

        self.root = Gio.ListStore(item_type=TxUser)
        self.tree = Gtk.TreeListModel.new(self.root, False, False,
                                          self._children, None)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup)
        factory.connect("bind", self._bind)
        factory.connect("unbind", self._unbind)
        lv = Gtk.ListView(model=Gtk.NoSelection(model=self.tree), factory=factory)
        enable_row_toggle(lv)
        lv.add_css_class("rich-list")

        self.pages = Gtk.Stack(vexpand=True)
        self.empty_desc = ("Files you queue from search results or a user's shares "
                           "appear here." if kind == "downloads" else
                           "Files other users download from your shares appear here.")
        self.empty = Placeholder(
            ("digger-download-symbolic" if kind == "downloads"
             else "digger-upload-symbolic"), f"No {kind}", self.empty_desc)
        self.pages.add_named(self.empty, "empty")
        self.pages.add_named(Gtk.ScrolledWindow(child=lv), "list")
        self.pages.set_visible_child_name("empty")
        self._build_sidebar()
        self.append(self.pages)

    @staticmethod
    def _children(item, *_):
        return item.children if item.kind != "file" else None

    # -- polling --
    def start(self):
        self.stop()
        self._poll()          # schedules the next poll itself when it completes

    def _interval(self):
        """Seconds until the next poll: fast only when you can see moving numbers."""
        host = self.host
        focused = host._window_focused() if host else True
        live = host._events_live() if host else False
        active = self.n_active > 0
        if self.is_visible() and focused:
            sec = 2 if active else 4
        elif focused:
            sec = 5 if active else 15        # still feeds the status bar
        else:
            sec = 10 if active else 30
        if live and not active:
            sec *= 2                         # events wake us when something finishes
        return min(60, sec * 2 ** min(self.fails, 3))

    def _schedule(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.get_api():
            self.timer = GLib.timeout_add_seconds(self._interval(), self._tick)

    def _tick(self):
        self.timer = 0
        self._poll()
        return False

    def stop(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        self.fails = 0
        self.n_active = 0
        self.users.clear()
        self.root.remove_all()
        self.loaded = False
        self.pending = None
        self.last_data = None
        self.items.clear()
        self.positions.clear()
        self.pos_pending.clear()
        self.bar.set_visible(False)
        self._set_counts({}, 0)
        self._prev, self._last_t, self._speed = {}, None, None
        self.session_bytes = 0
        self.pages.set_visible_child_name("empty")

    def poll(self):
        if self.pending is not None and self.is_visible():
            data, self.pending = self.pending, None
            self.update(data)
        self._poll()

    def _sample(self, data):
        """Derive live speed / counts from successive polls (for the status bar)."""
        now = time.monotonic()
        dt = (now - self._last_t) if self._last_t else None
        cur, speed, delta_total = {}, 0.0, 0
        index = {} if self.kind == "downloads" else None   # (user, file) -> state
        counts = collections.Counter()
        active = queued = total = 0
        for u in data:
            user_l = (u.get("username") or "").lower()
            for d in u.get("directories", []):
                for f in d.get("files", []):
                    if f.get("removed"):      # only counted under "Removed"
                        counts["removed"] += 1
                        continue
                    total += 1
                    state = f.get("state") or ""
                    if index is not None:
                        index[(user_l, f.get("filename"))] = state
                    counts[tx_category(state)] += 1
                    if state == "InProgress":
                        active += 1
                        fid, got = f.get("id"), f.get("bytesTransferred") or 0
                        cur[fid] = got
                        prev = self._prev.get(fid)
                        if prev is not None and dt:
                            delta = max(0, got - prev)
                            speed += delta / dt
                            delta_total += delta
                        else:
                            speed += f.get("averageSpeed") or 0
                    elif state.startswith("Queued") or state in ("Requested", "Initializing"):
                        queued += 1
        self._prev, self._last_t = cur, now
        if index is not None and self.on_index:
            self.on_index(index)
        self._set_counts(counts, total)
        self.n_active = active
        self.session_bytes += delta_total
        if not active:
            self._speed = 0.0
        else:
            self._speed = speed if self._speed is None else 0.5 * self._speed + 0.5 * speed
        if self.on_stats:
            self.on_stats(self.kind, {"speed": self._speed, "active": active,
                                      "queued": queued, "total": total,
                                      "session": self.session_bytes})

    def _poll(self, *_):
        api = self.get_api()
        if not api:
            self.timer = 0
            return False
        if self.busy:
            self._again = True      # a refresh is in flight; run another right after
            return False
        self.busy = True

        def rerun():
            if getattr(self, "_again", False):
                self._again = False
                GLib.idle_add(lambda: self._poll() and False)

        def ok(data):
            self.busy = False
            self.fails = 0
            self._schedule()
            rerun()
            if api is not self.get_api():
                return
            data = self._without_hidden(data)
            self._sample(data)
            if self.is_visible() or not self.loaded:
                self.pending = None
                self.update(data)
            else:
                self.pending = data   # applied when the tab is opened

        def err(_e):
            self.busy = False
            self.fails += 1
            self._schedule()
            rerun()

        run_async(lambda: api.transfers(self.kind, True), ok, err)
        return False

    # -- model update (in place, so expansion/scroll state is kept) --
    @staticmethod
    def _remove(store, item):
        found, pos = store.find(item)
        if found:
            store.remove(pos)

    def update(self, data):
        self.loaded = True
        seen = set()
        self.last_data = data
        # header buttons only make sense when there are live (non-removed) transfers
        flt = self.filter
        self.removed_title.set_visible(flt == "removed")
        self.bar.set_visible(flt != "removed" and any(
            not f.get("removed")
            for u in data for d in u.get("directories", []) for f in d.get("files", [])))
        for u in data:
            name = u.get("username")
            if not name:
                continue
            for d in u.get("directories", []):
                path = d.get("directory", "")
                for f in d.get("files", []):
                    cat = tx_cat(f)
                    if (cat == "removed") if flt == "all" else (cat != flt):
                        continue
                    user = self.users.get(name)
                    if user is None:
                        user = self.users[name] = TxUser(name)
                        self.root.insert_sorted(user, cmp_tx)
                    dr = user.dirs.get(path)
                    if dr is None:
                        dr = user.dirs[path] = TxDir(path)
                        user.children.insert_sorted(dr, cmp_tx)
                    fid = f.get("id")
                    seen.add(fid)
                    item = dr.files.get(fid)
                    if item is None:
                        item = dr.files[fid] = TxFile(f)
                        item.position = self.positions.get(fid, (0, None))[1]
                        self.items[fid] = item
                        dr.children.insert_sorted(item, cmp_tx)
                    else:
                        item.set_data(f)

        for name, user in list(self.users.items()):
            for path, dr in list(user.dirs.items()):
                for fid, item in list(dr.files.items()):
                    if fid not in seen:
                        del dr.files[fid]
                        self.items.pop(fid, None)
                        self._remove(dr.children, item)
                if not dr.files:
                    del user.dirs[path]
                    self._remove(user.children, dr)
            if not user.dirs:
                del self.users[name]
                self._remove(self.root, user)

        for user in self.users.values():
            for dr in user.dirs.values():
                for item in dr.files.values():
                    item.emit("changed")
                dr.emit("changed")
            user.emit("changed")
        if not self.users:
            if data and flt != "all":
                self.empty.set_title("No matching transfers")
                self.empty.set_description("Nothing has this status right now.")
            else:
                self.empty.set_title(f"No {self.kind}")
                self.empty.set_description(self.empty_desc)
        self.pages.set_visible_child_name("list" if self.users else "empty")
        self._request_positions()

    def _request_positions(self):
        """Look up the remote queue position of queued downloads (throttled)."""
        if self.kind != "downloads" or not self.is_visible():
            return
        api = self.get_api()
        if not api:
            return
        now = time.time()
        for fid, item in list(self.items.items()):
            if self.pos_active >= 3:
                break
            if (not item.state.startswith("Queued, Remotely") or fid in self.pos_pending
                    or now - self.positions.get(fid, (0, None))[0] < 90):
                continue
            self.pos_pending.add(fid)
            self.pos_active += 1

            def done(pos, i=fid):
                self.pos_active -= 1
                self.pos_pending.discard(i)
                self.positions[i] = (time.time(), pos)
                it = self.items.get(i)
                if it is not None and pos is not None:
                    it.position = pos
                    it.emit("changed")

            run_async(lambda u=item.username, i=fid: api.download_position(u, i),
                      done, lambda _e, i=fid: done(None, i))

    # -- status filter sidebar --
    STATUS_FILTERS = [
        ("all", "All", "digger-list-symbolic"),
        ("active", "In Progress", "digger-refresh-symbolic"),
        ("queued", "Queued", "digger-clock-symbolic"),
        ("completed", "Completed", "digger-ok-symbolic"),
        ("errored", "Errored", "digger-important-symbolic"),
        ("cancelled", "Cancelled", "digger-stop-symbolic"),
        ("removed", "Removed", "digger-trash-symbolic"),
    ]

    def _build_sidebar(self):
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title = Gtk.Label(label="Status", xalign=0, margin_top=18,
                          margin_bottom=8, margin_start=18, margin_end=6)
        title.add_css_class("heading")
        side.append(title)
        self.status_list = Gtk.ListBox()
        self.status_list.add_css_class("navigation-sidebar")
        self.status_rows, self.status_badges = {}, {}
        for key, label, icon in self.STATUS_FILTERS:
            row, badge = make_sidebar_row(label, icon)
            badge.remove_css_class("unread-badge")
            for c in ("dim-label", "caption", "numeric"):
                badge.add_css_class(c)
            badge.set_text("0")
            badge.set_visible(True)
            row._cat = key
            self.status_list.append(row)
            self.status_rows[key], self.status_badges[key] = row, badge
        def header(row, _before):
            # a separator sets "Removed" apart from the live statuses
            row.set_header(Gtk.Separator(margin_top=6, margin_bottom=6)
                           if row._cat == "removed" else None)

        self.status_list.set_header_func(header)
        self.status_list.connect("row-selected", self._on_status_selected)
        self.status_list.connect(
            "row-activated",
            lambda *_: self.host._hide_sidebar_if_collapsed() if self.host else None)
        self.status_list.select_row(self.status_rows["all"])
        side.append(self.status_list)
        self.sidebar = side

    def _on_status_selected(self, _lb, row):
        if row is None:
            return
        self.filter = row._cat
        if self.last_data is not None:
            self.update(self.last_data)

    def _set_counts(self, counts, total):
        for key, badge in self.status_badges.items():
            badge.set_text(str(total if key == "all" else counts.get(key, 0)))

    # -- header actions: Retry / Cancel / Remove with the slskd web UI's options --
    OPS = {
        "retry": ("Retry", [("errored", "Errored", "Retry All Errored"),
                            ("cancelled", "Cancelled", "Retry All Cancelled"),
                            ("all", "All", "Retry All")]),
        "cancel": ("Cancel", [("all", "All", "Cancel All"),
                              ("queued", "Queued", "Cancel All Queued"),
                              ("progress", "In Progress", "Cancel All In Progress")]),
        "remove": ("Remove", [("succeeded", "Succeeded", "Remove All Succeeded"),
                              ("errored", "Errored", "Remove All Errored"),
                              ("cancelled", "Cancelled", "Remove All Cancelled"),
                              ("completed", "Completed", "Remove All Completed")]),
    }
    ERRORED = ("Completed, TimedOut", "Completed, Errored", "Completed, Rejected")

    @classmethod
    def state_matches(cls, op, option, state):
        st = state or ""
        done = st.startswith("Completed")
        if op == "retry":
            if option == "errored":
                return st in cls.ERRORED
            if option == "cancelled":
                return st == "Completed, Cancelled"
            return done and "Succeeded" not in st
        if op == "cancel":
            if option == "queued":
                return st in ("Queued, Locally", "Queued, Remotely")
            if option == "progress":
                return st == "InProgress"
            return not done
        if option == "succeeded":
            return st == "Completed, Succeeded"
        if option == "errored":
            return st in cls.ERRORED
        if option == "cancelled":
            return st == "Completed, Cancelled"
        return done

    def _build_actions(self):
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                           max_children_per_line=3, column_spacing=8, row_spacing=6,
                           margin_top=12, margin_bottom=6, margin_start=12,
                           margin_end=12)
        group = Gio.SimpleActionGroup()
        self.op_buttons, self.op_choice = {}, {}
        ops = ["retry", "cancel", "remove"] if self.kind == "downloads" \
            else ["cancel", "remove"]       # uploads can't be retried
        for op in ops:
            options = self.OPS[op][1]
            self.op_choice[op] = options[0][0]
            menu = Gio.Menu()
            for key, label, _full in options:
                item = Gio.MenuItem.new(label, None)
                item.set_action_and_target_value(f"tx.{op}", GLib.Variant("s", key))
                menu.append_item(item)
            action = Gio.SimpleAction.new_stateful(
                op, GLib.VariantType.new("s"), GLib.Variant("s", options[0][0]))
            action.connect("activate", self._on_op_option, op)
            group.add_action(action)
            btn = Adw.SplitButton(label=options[0][2], menu_model=menu)
            btn.connect("clicked", self._on_op_clicked, op)
            self.op_buttons[op] = btn
            flow.append(btn)
        self.insert_action_group("tx", group)
        self.op_flow = flow
        self._apply_op_labels()
        return flow

    def _apply_op_labels(self):
        """Wide: text buttons. Phone layout: icon-only so they fit on one row."""
        for op, btn in self.op_buttons.items():
            full = next((f for k, _l, f in self.OPS[op][1] if k == self.op_choice[op]),
                        self.OPS[op][0])
            if TransfersView.compact:
                btn.set_icon_name(self.OP_ICONS[op])
                btn.set_tooltip_text(full)
            else:
                btn.set_label(full)
                btn.set_tooltip_text(None)
        if hasattr(self, "op_flow"):
            c = TransfersView.compact
            self.op_flow.set_margin_top(4 if c else 12)
            self.op_flow.set_margin_bottom(2 if c else 6)

    def _on_op_option(self, action, param, op):
        key = param.get_string()
        action.set_state(param)
        self.op_choice[op] = key
        self._apply_op_labels()

    def _on_op_clicked(self, _btn, op):
        self._run_bulk(op, self.op_choice[op])

    def _set_busy(self, busy):
        self.op_busy = busy
        for b in self.op_buttons.values():
            b.set_sensitive(not busy)

    def _all_files(self):
        for u in self.last_data or []:
            for d in u.get("directories", []):
                for f in d.get("files", []):
                    if not f.get("removed"):
                        yield (u.get("username") or f.get("username") or ""), f

    def _without_hidden(self, data):
        """Drop transfers that are being removed, so they vanish from the list at once."""
        if not self.hidden_ids or not isinstance(data, list):
            return data
        out = []
        for u in data:
            dirs = []
            for d in u.get("directories", []):
                files = [f for f in d.get("files", []) if f.get("id") not in self.hidden_ids]
                if files:
                    dirs.append(dict(d, files=files))
            if dirs:
                out.append(dict(u, directories=dirs))
        return out

    def _show_progress(self, done, total, verb):
        if total < 6:                      # quick enough not to need a bar
            return False
        self.prog_box.set_visible(done < total)
        self.prog_label.set_text(f"{verb} {done} of {total}…")
        self.prog_bar.set_fraction(done / total)
        return False

    def _each(self, items, fn, verb):
        """Run fn(item) for every item, eight at a time, reporting progress.
        Returns how many failed. Called from a worker thread."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        total, done, failed = len(items), 0, 0

        def one(it):
            try:
                fn(it)
                return True
            except Exception as e:  # noqa: BLE001
                log.warning("%s failed: %s", verb, e)
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            for fut in as_completed([pool.submit(one, it) for it in items]):
                done += 1
                failed += 0 if fut.result() else 1
                GLib.idle_add(self._show_progress, done, total, verb)
        GLib.idle_add(self.prog_box.set_visible, False)
        return failed

    def _run_bulk(self, op, option):
        api, kind = self.get_api(), self.kind
        if not api or self.op_busy:
            return
        files = [(user, f) for user, f in self._all_files()
                 if self.state_matches(op, option, f.get("state"))]
        verb = {"retry": "Retried", "cancel": "Cancelled", "remove": "Removed"}[op]
        if not files:
            self.toast(f"Nothing to {op}")
            return
        log.info("%s %d %s (%s)", op, len(files), kind, option)
        self._set_busy(True)

        def work():
            failed = 0
            all_done = sum(1 for _u, f in self._all_files()
                           if (f.get("state") or "").startswith("Completed"))
            if op == "remove" and (option == "completed" or
                                   (option == "succeeded" and all_done == len(files))):
                api.clear_completed(kind)       # one call instead of one per item
            elif op == "retry":
                by_user = {}
                for user, f in files:
                    by_user.setdefault(user, []).append(
                        {"filename": f.get("filename"), "size": f.get("size", 0)})
                for user, payload in by_user.items():
                    for i in range(0, len(payload), 500):
                        chunk = payload[i:i + 500]
                        try:
                            api.download(user, chunk)
                        except Exception as e:  # noqa: BLE001
                            log.warning("retry for %s failed: %s", user, e)
                            failed += len(chunk)
            else:
                failed += self._each(
                    files, lambda uf: api.cancel_transfer(
                        kind, uf[0], uf[1]["id"], remove=(op == "remove")),
                    "Removing" if op == "remove" else "Cancelling")
            return failed

        def done(failed):
            self._set_busy(False)
            n = len(files)
            msg = f"{verb} {n} transfer{'s' if n != 1 else ''}"
            if failed:
                msg += f" ({failed} failed)"
            self.toast(msg)
            self._poll()

        def err(e):
            self._set_busy(False)
            self.toast(describe_error(e))

        run_async(work, done, err)

    # -- rows --
    def _setup(self, _f, li):
        exp = Gtk.TreeExpander()
        row = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6,
                      margin_start=4, margin_end=12)
        if self.host:
            avatar, icon, dot = avatar_overlay(self.host.user_dir)
        else:
            avatar = icon = Gtk.Image()
            dot = None
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                         spacing=2)
        t = Gtk.Label(xalign=0, ellipsize=3)
        s = Gtk.Label(xalign=0, ellipsize=3)
        s.add_css_class("dim-label")
        s.add_css_class("caption")
        labels.append(t)
        labels.append(s)
        bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER,
                              width_request=self.BAR_COMPACT if self.compact
                              else self.BAR_WIDE)
        bar.add_css_class("tx-bar")
        self.bars.add(bar)
        btn = Gtk.Button(icon_name="digger-close-symbolic",
                         valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        browse = Gtk.Button(icon_name="digger-folder-open-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Browse this user's files")
        browse.add_css_class("flat")
        chat = Gtk.Button(icon_name="digger-chat-symbolic",
                          valign=Gtk.Align.CENTER, tooltip_text="Chat with this user")
        chat.add_css_class("flat")
        retry = Gtk.Button(icon_name="digger-refresh-symbolic", valign=Gtk.Align.CENTER,
                           tooltip_text="Retry", visible=False)
        retry.add_css_class("flat")
        avatar.set_size_request(ICON_SIZE, -1)
        for w in (avatar, labels, bar, browse, chat, retry, btn):
            row.append(w)
        exp.set_child(row)
        exp._w = (icon, t, s, bar, btn)
        exp._chat = chat
        exp._chid = 0
        exp._retry = retry
        exp._rhid = 0
        exp._browse = browse
        exp._dot = dot
        exp._hid = exp._cid = exp._bhid = 0
        exp._item = None
        if self.host:
            self.host.attach_user_tooltip(
                row, lambda: (exp._item.title, None)
                if exp._item is not None and exp._item.kind == "user" else None)
        li.set_child(exp)

    def _fill(self, exp, item):
        icon, t, s, bar, btn = exp._w
        exp._browse.set_visible(item.kind == "user" and self.host is not None)
        exp._chat.set_visible(item.kind == "user" and self.host is not None)
        t.remove_css_class("heading")
        if item.kind == "user":
            set_item_icon(icon, "user")
            t.add_css_class("heading")
            t.set_ellipsize(3)
            btn.set_visible(False)
        elif item.kind == "dir":
            set_item_icon(icon, "dir")
            t.set_ellipsize(2)  # MIDDLE: keep start and end visible
            btn.set_visible(True)
            btn.set_tooltip_text("Stop and remove all files in this folder")
        else:
            set_item_icon(icon, "file", item.title)
            t.set_ellipsize(2)
            btn.set_visible(True)
            btn.set_tooltip_text("Remove" if item.completed else "Cancel")
        exp._retry.set_visible(
            self.kind == "downloads" and item.kind != "user"
            and any(self.state_matches("retry", "all", f.state) for f in item.leaves()))
        t.set_text(item.title)
        if item.kind == "file":
            t.set_tooltip_markup(item.detail_markup())
        else:
            t.set_tooltip_text(item.tooltip)
        s.set_text(item.subtitle)
        bar.set_fraction(item.fraction)
        if exp._dot is not None:
            exp._dot.set_user(item.title if item.kind == "user" else None)

    def _bind(self, _f, li):
        exp, tl = li.get_child(), li.get_item()
        exp.set_list_row(tl)
        item = tl.get_item()
        exp._item = item
        self._fill(exp, item)
        exp._cid = item.connect("changed", lambda it: self._fill(exp, it))
        if item.kind != "user":
            exp._hid = exp._w[4].connect("clicked", self._on_cancel, item)
            exp._rhid = exp._retry.connect("clicked", self._on_retry, item)
        elif self.host:
            exp._bhid = exp._browse.connect("clicked", self._on_browse, item)
            exp._chid = exp._chat.connect(
                "clicked", lambda _b, it=item: self.host.show_chat(it.title))

    def _unbind(self, _f, li):
        exp = li.get_child()
        if exp._item is not None and exp._cid:
            exp._item.disconnect(exp._cid)
        if exp._hid:
            exp._w[4].disconnect(exp._hid)
        if exp._bhid:
            exp._browse.disconnect(exp._bhid)
        if exp._rhid:
            exp._retry.disconnect(exp._rhid)
        if exp._chid:
            exp._chat.disconnect(exp._chid)
        exp._hid = exp._cid = exp._bhid = exp._rhid = exp._chid = 0
        exp._item = None
        exp.set_list_row(None)

    # -- actions --
    def _on_retry(self, _btn, item):
        api = self.get_api()
        files = [f for f in item.leaves() if self.state_matches("retry", "all", f.state)]
        if not api or not files:
            return
        by_user = {}
        for f in files:
            by_user.setdefault(f.username, []).append(
                {"filename": f.filename, "size": f.size})

        def work():
            for user, payload in by_user.items():
                api.download(user, payload)

        run_async(work,
                  lambda _: (self.toast(f"Retrying {len(files)} "
                                        f"file{'s' if len(files) != 1 else ''}"),
                             self._poll()),
                  lambda e: self.toast(describe_error(e)))

    def _on_browse(self, _btn, item):
        self.host.browse_user(item.title)

    def _cancel_files(self, files, remove_all=False):
        api, kind = self.get_api(), self.kind
        targets = [(f.username, f.id, f.completed or remove_all) for f in files]
        if not targets:
            return

        # removals disappear from the list immediately; the server catches up behind
        gone = {tid for _u, tid, remove in targets if remove}
        if gone:
            self.hidden_ids |= gone
            if self.last_data is not None:
                self.last_data = self._without_hidden(self.last_data)
                self.update(self.last_data)

        def work():
            failed = self._each(
                targets, lambda t: api.cancel_transfer(kind, t[0], t[1], remove=t[2]),
                "Removing" if remove_all else "Cancelling")
            if failed:
                raise RuntimeError(f"{failed} of {len(targets)} could not be "
                                   "cancelled or removed")

        def done(_r=None):
            self.hidden_ids -= gone
            self._poll()
            # stopping an active transfer can take a moment on the server
            GLib.timeout_add(1500, lambda: self._poll() and False)

        def fail(e):
            self.hidden_ids -= gone         # show what is really left
            self.toast(describe_error(e))
            self._poll()

        run_async(work, done, fail)

    def _on_cancel(self, _btn, item):
        # a folder is stopped and removed completely; a single file is only cancelled
        self._cancel_files(item.leaves(), remove_all=item.kind == "dir")
