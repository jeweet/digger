"""Main window: search tab."""

import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from gi.repository import Adw, Gio, GLib, Gtk

from .constants import ICON_SIZE, MAX_RESULTS, MAX_SHOWN, SORT_MODES
from .logger import log
from .util import describe_error, split_path
from .icons import set_item_icon
from .asyncutil import run_async
from .widgets import (
    Placeholder, avatar_overlay, clear_children, enable_row_toggle,
    make_sidebar_row, sidebar_action, sidebar_placeholder,
)
from .models import (
    DIR_CACHE, DirItem, FileItem, SearchFilter, UserItem, dir_cache_key,
    make_user_item,
)


class SearchMixin:
    # ---- search tab -----------------------------------------------------
    def _build_search(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # result filters: same syntax and options as the slskd web UI
        self.filter_entry = Gtk.SearchEntry(
            placeholder_text="Filter results — e.g. flac -live minbitrate:320",
            hexpand=True)
        self.filter_entry.connect("search-changed", self._on_filters_changed)
        help_btn = self.help_btn = Gtk.MenuButton(
            icon_name="digger-info-symbolic", tooltip_text="Filter syntax",
            valign=Gtk.Align.CENTER, popover=self._build_filter_help())
        help_btn.add_css_class("flat")
        # A popover next to a focused entry is closed again by the on-screen
        # keyboard on touch screens; on the phone layout a dialog is used instead.
        self.help_btn_touch = Gtk.Button(icon_name="digger-info-symbolic",
                                         tooltip_text="Filter syntax",
                                         valign=Gtk.Align.CENTER, visible=False)
        self.help_btn_touch.add_css_class("flat")
        self.help_btn_touch.connect("clicked", lambda *_: self._show_filter_help())
        labels = [l for _k, l in SORT_MODES]
        self._sync = False
        self.sort_dd = Gtk.DropDown.new_from_strings(labels)
        self.sort_dd.set_tooltip_text("Sort users by")
        self.sort_dd.connect("notify::selected", self._on_sort_changed)
        self.free_check = Gtk.CheckButton(label="Free slots only")
        self.free_check.connect("toggled", self._on_free_toggled)

        # startup defaults from Settings
        cfg = getattr(self, "cfg", None) or {}
        modes = [k for k, _l in SORT_MODES]
        # phone layout: the same two controls live in a popover instead
        self.sort_dd2 = Gtk.DropDown.new_from_strings(labels)
        self.sort_dd2.connect("notify::selected", self._on_sort_changed)
        self.free_check2 = Gtk.CheckButton(label="Free slots only")
        self.free_check2.connect("toggled", self._on_free_toggled)
        self._sync = True
        if cfg.get("default_sort") in modes:
            idx = modes.index(cfg["default_sort"])
            self.sort_dd.set_selected(idx)
            self.sort_dd2.set_selected(idx)
            self.sort_mode = cfg["default_sort"]
        self.free_check.set_active(bool(cfg.get("default_free_only")))
        self.free_check2.set_active(bool(cfg.get("default_free_only")))
        self._sync = False
        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                          margin_top=10, margin_bottom=10, margin_start=10,
                          margin_end=10)
        pop_box.append(Gtk.Label(label="Sort by", xalign=0))
        pop_box.append(self.sort_dd2)
        pop_box.append(self.free_check2)
        self.opts_btn = Gtk.MenuButton(icon_name="digger-sort-symbolic",
                                       tooltip_text="Sort and filter options",
                                       popover=Gtk.Popover(child=pop_box),
                                       valign=Gtk.Align.CENTER, visible=False)
        self.opts_btn.add_css_class("flat")

        self.redo_btn = Gtk.Button(icon_name="digger-refresh-symbolic",
                                   tooltip_text="Search again", visible=False)
        self.redo_btn.connect("clicked", lambda *_: self._redo_search())

        frow = self.filter_row = Gtk.Box(spacing=8, margin_top=12, margin_start=12,
                                         margin_end=12, visible=False)
        for w in (self.filter_entry, help_btn, self.help_btn_touch, self.sort_dd,
                  self.free_check, self.opts_btn):
            frow.append(w)
        self.sort_dd.set_valign(Gtk.Align.CENTER)
        self.free_check.set_valign(Gtk.Align.CENTER)
        box.append(frow)

        # users (sorted) -> directories -> files
        self.results = Gio.ListStore(item_type=UserItem)
        self.user_sorter = Gtk.CustomSorter.new(self._compare_users, None)
        sorted_users = Gtk.SortListModel(model=self.results,
                                         sorter=self.user_sorter)
        # users open by default (so folders show), folders stay collapsed
        self.tree = Gtk.TreeListModel.new(sorted_users, False, False,
                                          self._tree_children, None)
        self.tree.connect("items-changed", self._expand_new_users)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._result_setup)
        factory.connect("bind", self._result_bind)
        factory.connect("unbind", self._result_unbind)
        lv = Gtk.ListView(model=Gtk.NoSelection(model=self.tree), factory=factory)
        enable_row_toggle(lv)
        lv.add_css_class("rich-list")
        self.search_stack = Gtk.Stack(vexpand=True)
        self.search_page = Placeholder("digger-search-symbolic")
        self.search_cancel = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER)
        self.search_cancel.add_css_class("pill")
        self.search_cancel.connect("clicked", lambda *_: self._cancel_search())
        self.search_stack.add_named(self.search_page, "empty")
        self.search_stack.add_named(Gtk.ScrolledWindow(child=lv), "list")
        box.append(self.search_stack)
        self._update_results_page()
        self.side_search = self._build_recents()
        return box

    # recent searches sidebar
    def _build_recents(self):
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.search_entry = Gtk.Entry(placeholder_text="Search query, then Enter",
                                      width_chars=24)
        self.search_entry.connect("activate", self._on_search)
        self.search_pop = Gtk.Popover(child=Gtk.Box(
            margin_top=6, margin_bottom=6, margin_start=6, margin_end=6))
        self.search_pop.get_child().append(self.search_entry)
        self.search_pop.connect("show", lambda *_: self.search_entry.grab_focus())
        new_btn = sidebar_action(self.search_pop, "New Search")
        new_btn.set_hexpand(True)
        new_btn.set_margin_end(0)

        # clear-all button; asks for confirmation in a dialog first
        self.clear_btn = Gtk.Button(icon_name="digger-trash-symbolic",
                                    tooltip_text="Delete all searches", margin_top=6,
                                    margin_bottom=2, margin_end=6, sensitive=False)
        self.clear_btn.add_css_class("flat")
        self.clear_btn.connect("clicked", lambda *_: self._confirm_delete_searches())
        top = Gtk.Box()
        top.append(new_btn)
        top.append(self.clear_btn)
        side.append(top)

        self.recent_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.recent_list.add_css_class("navigation-sidebar")
        self.recent_list.set_placeholder(sidebar_placeholder("No searches"))
        self.recent_list.connect("row-activated", self._on_recent_activated)
        side.append(Gtk.ScrolledWindow(vexpand=True, child=self.recent_list))
        self._refresh_recents()
        return side

    def _load_searches(self):
        api = self.api
        if not api:
            return

        def ok(lst):
            if api is not self.api:
                return
            lst = [x for x in (lst or []) if isinstance(x, dict) and x.get("id")]
            lst.sort(key=lambda x: x.get("startedAt") or "", reverse=True)
            self.searches = lst
            self._refresh_recents()

        run_async(api.searches, ok, lambda e: log.debug("search list failed: %s", e))

    def _refresh_recents(self):
        self.clear_btn.set_sensitive(bool(self.searches))
        sig = [(x.get("id"), x.get("searchText"), x.get("isComplete"),
                x.get("fileCount"), x.get("responseCount")) for x in self.searches]
        if sig == self._search_sig:
            self._select_current_recent()
            return
        self._search_sig = sig
        clear_children(self.recent_list)
        self.recent_rows = {}
        for sd in self.searches:
            sid, text = sd["id"], sd.get("searchText") or "(empty)"
            row, _badge = make_sidebar_row(
                text, "digger-clock-symbolic",
                lambda sid=sid: self._delete_search(sid), "Delete search")
            row._query, row._sid, row._data = text, sid, sd
            self.recent_list.append(row)
            self.recent_rows[sid] = row
        self._update_recent_tooltips()
        self._select_current_recent()

    def _select_current_recent(self):
        row = self.recent_rows.get(self.search_id) if self.search_started else None
        if row is not None:
            self.recent_list.select_row(row)
        else:
            self.recent_list.unselect_all()

    @staticmethod
    def _stats_text(sd):
        files = sd.get("fileCount") or 0
        text = f"{files:,} files from {sd.get('responseCount') or 0:,} users"
        if sd.get("lockedFileCount"):
            text += f" ({sd['lockedFileCount']:,} locked)"
        if not sd.get("isComplete"):
            text += "\nIn progress"
        elif sd.get("state"):
            text += f"\n{sd['state']}"
        if sd.get("startedAt"):
            try:
                when = GLib.DateTime.new_from_iso8601(sd["startedAt"], None).to_local()
                text += "\nSearched " + when.format("%b %e, %Y %H:%M").replace("  ", " ")
            except Exception:  # noqa: BLE001
                pass
        return text

    def _update_recent_tooltips(self):
        for row in self.recent_rows.values():
            row._label.set_tooltip_text(f"{row._query}\n{self._stats_text(row._data)}")

    def _close_search(self):
        """Drop the displayed results (and stop the search if still running)."""
        api, sid = self.api, self.search_id
        if self.searching and api and sid:
            run_async(lambda: api.cancel_search(sid), None,
                      lambda e: log.debug("cancel failed: %s", e))
        self.search_gen += 1
        self.results.remove_all()
        self.raw, self.raw_files, self.searching = [], 0, False
        self.raw_dirs = 0
        self.current_query = ""
        self.search_started = False
        self.search_cancelled = False
        self._update_results_page()
        self._update_status()

    def _delete_search(self, sid):
        api = self.api
        if not api:
            return
        if self.search_started and sid == self.search_id:
            self._close_search()              # also cancels it if still running
        self.searches = [x for x in self.searches if x.get("id") != sid]
        self._refresh_recents()

        def done(_r=None):
            self._load_searches()

        def err(e):
            self.toast(describe_error(e))
            self._load_searches()

        run_async(lambda: api.delete_search(sid), done, err)

    def _confirm_delete_searches(self):
        n = len(self.searches)
        if not n:
            return
        heading = f"Delete {'all ' if n != 1 else ''}{n} search{'es' if n != 1 else ''}?"
        body = "They are removed from the slskd server too, including in the web UI."
        if hasattr(Adw, "AlertDialog"):
            dlg = Adw.AlertDialog(heading=heading, body=body)
        else:
            dlg = Adw.MessageDialog(transient_for=self, heading=heading, body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete All")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda _d, r: self._delete_all_searches()
                    if r == "delete" else None)
        if hasattr(Adw, "AlertDialog"):
            dlg.present(self)
        else:
            dlg.present()

    def _delete_all_searches(self):
        api = self.api
        if not api or not self.searches:
            return
        ids = [x["id"] for x in self.searches]
        log.info("Deleting %d searches", len(ids))
        if self.search_started:
            self._close_search()
        self.searches = []
        self._refresh_recents()

        def work():
            failed = 0
            for sid in ids:
                try:
                    api.delete_search(sid)
                except Exception as e:  # noqa: BLE001
                    log.warning("delete of search %s failed: %s", sid, e)
                    failed += 1
            return failed

        def done(failed):
            if failed:
                self.toast(f"{failed} search{'es' if failed != 1 else ''} "
                           "could not be deleted")
            self._load_searches()

        run_async(work, done, lambda e: self.toast(describe_error(e)))

    def _on_recent_activated(self, _lb, row):
        self._open_search(row._data)
        self._hide_sidebar_if_collapsed()

    def _open_search(self, sd):
        """Show the results of a search that already exists on the server."""
        if self.search_started and sd["id"] == self.search_id:
            return
        started = 0
        try:
            started = GLib.DateTime.new_from_iso8601(sd.get("startedAt"), None).to_unix()
        except Exception:  # noqa: BLE001
            pass
        self._begin_search(sd.get("searchText") or "", sd["id"], start=False,
                           started=started)

    def _expand_new_users(self, model, pos, _removed, added):
        if not added:
            return

        def go():
            # backwards, so expanding a row doesn't shift the ones still to do
            for i in reversed(range(pos, min(pos + added, model.get_n_items()))):
                row = model.get_row(i)
                if row is not None and isinstance(row.get_item(), UserItem) \
                        and not row.get_expanded():
                    row.set_expanded(True)
            return False
        GLib.idle_add(go)

    @staticmethod
    def _tree_children(item, *_):
        if isinstance(item, (UserItem, DirItem)):
            return item.children
        return None

    def _result_setup(self, _f, li):
        expander = Gtk.TreeExpander()
        row = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6,
                      margin_start=4, margin_end=12)
        avatar, icon, dot = avatar_overlay(self.user_dir)
        avatar.set_size_request(ICON_SIZE, -1)   # keeps the text column aligned
        mark = Gtk.Image(valign=Gtk.Align.CENTER, visible=False)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                         spacing=2)
        t = Gtk.Label(xalign=0, ellipsize=3)  # END
        s = Gtk.Label(xalign=0, ellipsize=3)
        s.add_css_class("dim-label")
        s.add_css_class("caption")
        labels.append(t)
        labels.append(s)
        browse = Gtk.Button(icon_name="digger-folder-open-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Browse this user's files")
        browse.add_css_class("flat")
        chat = Gtk.Button(icon_name="digger-chat-symbolic",
                          valign=Gtk.Align.CENTER, tooltip_text="Chat with this user")
        chat.add_css_class("flat")
        btn = Gtk.Button(icon_name="digger-download-symbolic",
                         valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        for w in (avatar, labels, mark, browse, chat, btn):
            row.append(w)
        expander.set_child(row)
        expander._xid = 0
        expander._xrow = None
        expander._w = (icon, t, s, btn)
        expander._browse = browse
        expander._chat = chat
        expander._chid = 0
        expander._dot = dot
        expander._mark = mark
        self._result_widgets.add(expander)
        expander._hid = expander._bhid = 0
        expander._item = None
        self.attach_user_tooltip(
            row, lambda: (expander._item.username, expander._item.tip_extra)
            if isinstance(expander._item, UserItem) else None)
        li.set_child(expander)

    def _result_bind(self, _f, li):
        exp, tl = li.get_child(), li.get_item()
        exp.set_list_row(tl)
        item = tl.get_item()
        exp._item = item
        icon, t, s, btn = exp._w
        t.remove_css_class("heading")
        exp._browse.set_visible(isinstance(item, UserItem))
        exp._chat.set_visible(isinstance(item, UserItem))
        exp._dot.set_user(item.username if isinstance(item, UserItem) else None)
        if isinstance(item, UserItem):
            set_item_icon(icon, "user")
            t.set_text(item.username)
            t.add_css_class("heading")
            t.set_ellipsize(3)
            t.set_tooltip_text(None)
            btn.set_visible(False)
            exp._bhid = exp._browse.connect(
                "clicked", lambda _b, it=item: self.browse_user(it.username))
            exp._chid = exp._chat.connect(
                "clicked", lambda _b, it=item: self.show_chat(it.username))
        elif isinstance(item, DirItem):
            set_item_icon(icon, "dir")
            t.set_text(item.title)
            t.set_ellipsize(2)  # MIDDLE: keep start and end visible
            t.set_tooltip_text(item.path)
            btn.set_visible(True)
            btn.set_tooltip_text("Download the whole folder")
            exp._xrow = tl
            exp._xid = tl.connect("notify::expanded", self._on_row_expanded)
        else:
            set_item_icon(icon, "file", item.title)
            t.set_text(item.title)
            t.set_ellipsize(2)
            t.set_tooltip_text(item.filename)
            btn.set_visible(True)
            btn.set_tooltip_text("Download file")
        s.set_text(item.subtitle)
        if isinstance(item, FileItem):
            exp._hid = btn.connect("clicked", self._on_download, item)
        elif isinstance(item, DirItem):
            exp._hid = btn.connect(
                "clicked", lambda _b, it=item: self.download_full_folder(
                    it.username, it.path))
        self._apply_mark(exp)

    def _on_row_expanded(self, row, _pspec):
        item = row.get_item()
        if not (row.get_expanded() and isinstance(item, DirItem)) \
                or item.loaded or item.loading or not self.api:
            return
        item.loading = True
        api = self.api
        self._set_dir_subtitle(item, "Loading folder…")

        def work():
            dirs = api.user_directory(item.username, item.path)
            want = dir_cache_key(item.username, item.path)[1]
            exact = [d for d in dirs
                     if (d.get("name") or "").lower().replace("/", "\\") == want]
            files = []
            for d in (exact or dirs):
                dname = d.get("name") or item.path
                for f in d.get("files") or []:
                    name = f.get("filename", "")
                    full = name if ("\\" in name or "/" in name) else f"{dname}\\{name}"
                    files.append(dict(f, filename=full))
            return files

        def ok(files):
            item.loading = False
            if files:
                item.loaded = True
                DIR_CACHE[dir_cache_key(item.username, item.path)] = files
                while len(DIR_CACHE) > 500:
                    DIR_CACHE.popitem(last=False)
                item.set_files([FileItem(item.username, f) for f in files])
                for exp in list(self._result_widgets):
                    if exp._item is item:
                        self._apply_mark(exp)
            self._set_dir_subtitle(item, item.subtitle)

        def err(e):
            # offline or unavailable: keep showing the files that matched
            item.loading = False
            log.debug("Folder listing failed: %s", e)
            self._set_dir_subtitle(item, item.subtitle)

        run_async(work, ok, err)

    def _set_dir_subtitle(self, item, text):
        for exp in list(self._result_widgets):
            if exp._item is item:
                exp._w[2].set_text(text)

    # -- "already in downloads" marks on search results --
    def _download_mark(self, item):
        if isinstance(item, FileItem):
            files = [item]
        elif isinstance(item, DirItem):
            files = item.files
        else:
            return None
        user = item.username.lower()
        present = [st for st in (self.dl_index.get((user, f.filename)) for f in files)
                   if st]
        if not present:
            return None
        n, k = len(files), len(present)
        tip = (f"Already in downloads: {present[0]}" if n == 1
               else f"{k} of {n} files already in downloads")
        if any(st == "InProgress" for st in present):
            return "digger-refresh-symbolic", "accent", tip
        if any(st.startswith("Queued") or st in ("Requested", "Initializing")
               for st in present):
            return "digger-clock-symbolic", "dim-label", tip
        if any(st.startswith("Completed") and not st.startswith("Completed, Succeeded")
               for st in present):
            return "digger-warning-symbolic", "warning", tip
        return "digger-ok-symbolic", ("success" if k == n else "dim-label"), tip

    def _apply_mark(self, exp):
        mark = exp._mark
        info = self._download_mark(exp._item) if exp._item is not None else None
        if info is None:
            mark.set_visible(False)
            return
        icon, cls, tip = info
        mark.set_from_icon_name(icon)
        for c in ("success", "warning", "accent", "dim-label"):
            mark.remove_css_class(c)
        mark.add_css_class(cls)
        mark.set_tooltip_text(tip)
        mark.set_visible(True)

    def _set_download_index(self, index):
        if index == self.dl_index:
            return
        self.dl_index = index
        for exp in list(self._result_widgets):
            if exp._item is not None:
                self._apply_mark(exp)

    def download_full_folder(self, username, directory):
        """Ask the user for the complete folder (search results only show matches)."""
        api = self.api
        if not api or not directory:
            return
        self.toast(f"Fetching the full folder from {username}…")

        def work():
            payload = []
            for d in api.user_directory(username, directory):
                dname = d.get("name") or directory
                for f in d.get("files") or []:
                    name = f.get("filename", "")
                    full = name if ("\\" in name or "/" in name) else f"{dname}\\{name}"
                    payload.append({"filename": full, "size": f.get("size", 0) or 0})
            return payload

        def ok(payload):
            if not payload:
                self.toast("The folder is empty or no longer available")
                return
            self.queue_download(username, payload)

        def err(e):
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                self.toast(f"{username} is offline")
            else:
                self.toast(describe_error(e))

        run_async(work, ok, err)

    def _result_unbind(self, _f, li):
        exp = li.get_child()
        if exp._xid:
            exp._xrow.disconnect(exp._xid)
            exp._xid = 0
            exp._xrow = None
        if exp._hid:
            exp._w[3].disconnect(exp._hid)
            exp._hid = 0
        if exp._bhid:
            exp._browse.disconnect(exp._bhid)
            exp._bhid = 0
        if exp._chid:
            exp._chat.disconnect(exp._chid)
            exp._chid = 0
        exp._item = None
        exp.set_list_row(None)

    def _on_download(self, _btn, item):
        api = self.api
        files = item.files if isinstance(item, DirItem) else [item]
        payload = [{"filename": f.filename, "size": f.size} for f in files]
        msg = (f"Queued {len(payload)} file{'s' if len(payload) != 1 else ''}"
               if isinstance(item, DirItem) else f"Queued: {item.title}")
        run_async(
            lambda: api.download(item.username, payload),
            lambda _: self.toast(msg),
            lambda e: self.toast(describe_error(e)),
        )

    def _compare_users(self, a, b, *_):
        ka, kb = a.sort_key(self.sort_mode), b.sort_key(self.sort_mode)
        return (ka > kb) - (ka < kb)

    def _on_sort_changed(self, dd, *_):
        if self._sync:
            return
        idx = dd.get_selected()
        self._sync = True
        for other in (self.sort_dd, self.sort_dd2):
            if other.get_selected() != idx:
                other.set_selected(idx)
        self._sync = False
        self.sort_mode = SORT_MODES[idx][0]
        self.user_sorter.changed(Gtk.SorterChange.DIFFERENT)

    def _on_free_toggled(self, check):
        if self._sync:
            return
        self._sync = True
        for other in (self.free_check, self.free_check2):
            other.set_active(check.get_active())
        self._sync = False
        self._on_filters_changed()

    def _on_filters_changed(self, *_):
        self.flt = SearchFilter(self.filter_entry.get_text(),
                                self.free_check.get_active())
        self._rebuild_results()

    FILTER_HELP = ("<b>Filter syntax</b> (same as the slskd web UI)\n\n"
                   "<tt>word</tt>   must appear in the file path\n"
                   "<tt>-word</tt>   must not appear\n"
                   "<tt>iscbr</tt> / <tt>isvbr</tt>   constant / variable bit rate\n"
                   "<tt>islossless</tt> / <tt>islossy</tt>\n"
                   "<tt>minbitrate:320</tt>   kbps\n"
                   "<tt>minbitdepth:24</tt>\n"
                   "<tt>minfilesize:10</tt>   megabytes\n"
                   "<tt>minlength:300</tt>   seconds\n"
                   "<tt>minfilesinfolder:8</tt>\n\n"
                   "Example: <tt>flac -live islossless minbitdepth:24</tt>")

    def _build_filter_help(self):
        label = Gtk.Label(label=self.FILTER_HELP, use_markup=True, xalign=0, wrap=True,
                          max_width_chars=46, margin_top=12, margin_bottom=12,
                          margin_start=12, margin_end=12)
        return Gtk.Popover(child=label)

    def _show_filter_help(self):
        """Same text as the popover, as a dialog (reliable on touch screens)."""
        body = self.FILTER_HELP.split("\n\n", 1)[1]
        if hasattr(Adw, "AlertDialog"):
            dlg = Adw.AlertDialog(heading="Filter syntax", body=body)
            dlg.set_body_use_markup(True)
        else:
            dlg = Adw.MessageDialog(transient_for=self, heading="Filter syntax",
                                    body=body)
            dlg.set_body_use_markup(True)
        dlg.add_response("ok", "Close")
        dlg.set_default_response("ok")
        dlg.set_close_response("ok")
        if hasattr(Adw, "AlertDialog"):
            dlg.present(self)
        else:
            dlg.present()

    def _build_items(self, resps):
        items = []
        for r in resps:
            if self.res_files >= MAX_SHOWN:
                break
            user = make_user_item(r, self.flt)
            if user is None:
                continue
            items.append(user)
            self.res_files += user.file_count
            self.res_dirs += user.dir_count
        return items

    def _rebuild_results(self):
        self.results.remove_all()
        self.res_files = self.res_dirs = 0
        items = self._build_items(self.raw)
        if items:
            self.results.splice(0, 0, items)
        self._update_status()

    def _sync_redo(self):
        btn = getattr(self, "redo_btn", None)
        vs = getattr(self, "viewstack", None)
        if btn is None or vs is None:
            return
        btn.set_visible(vs.get_visible_child_name() == "search"
                        and getattr(self, "search_started", False)
                        and not getattr(self, "searching", True)
                        and bool(getattr(self, "current_query", "")))

    def _redo_search(self):
        """Run the current search again; the old entry is replaced on the server."""
        api, old, text = self.api, self.search_id, self.current_query
        if not api or not text or self.searching:
            return
        self._begin_search(text, str(uuid.uuid4()), start=True)
        if old:
            run_async(lambda: api.delete_search(old), None,
                      lambda e: log.debug("delete of old search failed: %s", e))

    def _update_results_page(self):
        self._sync_redo()
        # filter / sort controls only make sense once there is something to filter;
        # they stay when filters hide every result, so they can be changed back
        self.filter_row.set_visible(self.raw_files > 0)
        if self.results.get_n_items():
            self.search_stack.set_visible_child_name("list")
            return
        hidden = self.raw_files - self.res_files
        if not self.search_started:
            icon, title, desc = ("digger-search-symbolic", "No search selected",
                                 "Select a search from the sidebar, or start a new one.")
        elif self.searching:
            icon, title, desc = (None, "Searching…",
                                 "Results appear here as they arrive.")
        elif self.search_cancelled and not self.raw_files:
            icon, title, desc = ("digger-search-symbolic", "Search cancelled",
                                 "Use New Search in the sidebar to start another.")
        elif hidden > 0:
            icon, title, desc = ("digger-search-symbolic", "No matching results",
                                 f"{hidden} files are hidden by the current filters.")
        else:
            icon, title, desc = ("digger-search-symbolic", "No results",
                                 "Try different search terms.")
        # while searching, the spinner takes the icon's place (same size/position)
        self.search_page.set_icon_name(icon)
        self.search_page.set_child(self.search_cancel if self.searching else None)
        self.search_page.set_title(title)
        self.search_page.set_description(desc)
        self.search_stack.set_visible_child_name("empty")

    def _update_status(self):
        self._update_results_page()

    def _cancel_search(self):
        if not self.searching:
            return
        api, sid = self.api, self.search_id
        log.info("Search cancelled")
        self.search_gen += 1              # stops the polling loop
        self.searching = False
        self.search_cancelled = True
        self._update_status()
        if api and sid:
            run_async(lambda: api.cancel_search(sid), None,
                      lambda e: log.debug("cancel failed: %s", e))

    def _on_search(self, entry):
        text = entry.get_text().strip()
        if not text or not self.api:
            return
        entry.set_text("")
        self.search_pop.popdown()
        self._hide_sidebar_if_collapsed()
        self._begin_search(text, str(uuid.uuid4()), start=True)

    def _begin_search(self, text, sid, start, started=None):
        """Start a new search (start=True) or load an existing one from the server."""
        self.current_query = text
        self.search_time = started or int(time.time())
        log.info("%s: %s", "Search" if start else "Opening search", text)
        self.search_gen += 1
        gen = self.search_gen
        api = self.api
        if start and self.searching and self.search_id:    # replacing a running search
            old = self.search_id
            run_async(lambda: api.cancel_search(old), None,
                      lambda e: log.debug("cancel of previous search failed: %s", e))
        self.search_id = sid
        self.search_cancelled = False
        DIR_CACHE.clear()
        self.results.remove_all()
        self.raw, self.raw_files, self.raw_dirs = [], 0, 0
        self.res_files = self.res_dirs = 0
        self.searching = True
        self.search_started = True
        self._update_results_page()

        seen = set()

        def push(resps, complete):
            if gen != self.search_gen:
                return False
            fresh = []
            for r in resps:
                n = len(r.get("files", []))
                if n and self.raw_files < MAX_RESULTS:
                    self.raw.append(r)
                    self.raw_files += n
                    self.raw_dirs += len({split_path(f.get("filename", ""))[0]
                                          for f in r.get("files", [])})
                    fresh.append(r)
            items = self._build_items(fresh)
            if items:
                self.results.splice(self.results.get_n_items(), 0, items)
            if complete:
                self.searching = False
            self._update_status()
            if complete:
                self._load_searches()     # refresh totals in the sidebar
            return False

        def fail(e):
            if gen == self.search_gen:
                self.searching = False
                self._update_results_page()
                self.toast(describe_error(e))

        def poll():
            if start:
                api.start_search(text, sid)
                GLib.idle_add(lambda: self._load_searches() or False)
            first = True
            while gen == self.search_gen:
                if start or not first:
                    time.sleep(1.5)
                first = False
                state = api.search_state(sid) or {}
                complete = bool(state.get("isComplete"))
                new = []
                for r in api.search_responses(sid):
                    key = (r.get("username"), r.get("token"))
                    if key not in seen:
                        seen.add(key)
                        new.append(r)
                GLib.idle_add(push, new, complete)
                if complete:
                    break

        run_async(poll, None, fail)
