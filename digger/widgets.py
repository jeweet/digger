"""Small reusable widgets and sidebar helpers."""

import weakref

from gi.repository import Adw, Gtk


def clear_children(widget):
    child = widget.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        widget.remove(child)
        child = nxt


class StatusDot(Gtk.Image):
    """Presence dot: green online, amber away, red offline, grey unknown."""
    COLORS = {"Online": "success", "Away": "warning", "Offline": "error"}
    _all = weakref.WeakSet()

    def __init__(self, directory, size=9):
        super().__init__(icon_name="digger-dot-symbolic", pixel_size=size,
                         valign=Gtk.Align.CENTER, visible=False)
        self.directory = directory
        self.username = None
        self._fetch = True
        StatusDot._all.add(self)

    def set_user(self, username, fetch=True):
        changed = username != self.username
        self.username, self._fetch = username, fetch
        if not username:
            self.set_visible(False)
            return
        self.set_visible(True)
        presence = self.directory.peek_status(username)
        if presence is not None:
            self._apply(presence)
        elif changed:
            self._apply(None)          # don't flicker when merely refreshing
        if presence is None and fetch:
            self._request()

    def _request(self):
        user = self.username
        self.directory.status(
            user, lambda p: self._apply(p) if user == self.username else None)

    def _apply(self, presence):
        for c in ("success", "warning", "error", "dim-label"):
            self.remove_css_class(c)
        cls = self.COLORS.get(presence)
        self.add_css_class(cls or "dim-label")
        self.set_opacity(1.0 if cls else 0.35)
        if cls:
            self.set_tooltip_text(presence)
        else:
            self.set_tooltip_text("Status unknown" if presence == "" else "Checking status…")

    @classmethod
    def refresh_all(cls):
        for dot in list(cls._all):
            if (dot.username and dot._fetch and dot.get_mapped()
                    and dot.directory.peek_status(dot.username) is None):
                dot._request()


def avatar_overlay(directory, icon_name="digger-user-symbolic", size=None,
                   dot_size=9):
    """Avatar icon with a presence dot in its bottom-right corner."""
    icon = Gtk.Image(icon_name=icon_name)
    if size:
        icon.set_pixel_size(size)
    dot = StatusDot(directory, size=dot_size)
    dot.set_halign(Gtk.Align.END)
    dot.set_valign(Gtk.Align.END)
    overlay = Gtk.Overlay(child=icon)
    overlay.add_overlay(dot)
    return overlay, icon, dot


def make_sidebar_row(text, icon, on_close=None, close_tip="Close", dot_dir=None):
    """Light 'navigation-sidebar' style row (icon, label, optional unread badge,
    and a close button that only shows on hover / selection / keyboard focus)."""
    row = Gtk.ListBoxRow()
    box = Gtk.Box(spacing=10, margin_top=4, margin_bottom=4)
    if dot_dir is not None:       # user rows get a presence dot on the avatar
        img, _icon, dot = avatar_overlay(dot_dir, icon)
        dot.set_user(text)
        row._dot = dot
    else:
        img = Gtk.Image(icon_name=icon)
    label = Gtk.Label(label=text, xalign=0, hexpand=True, ellipsize=3,
                      tooltip_text=text)
    badge = Gtk.Label(visible=False, valign=Gtk.Align.CENTER)
    badge.add_css_class("unread-badge")
    row._label = label
    box.append(img)
    box.append(label)
    box.append(badge)
    if on_close:
        close = Gtk.Button(icon_name="digger-close-symbolic",
                           tooltip_text=close_tip, valign=Gtk.Align.CENTER,
                           opacity=0)
        close.add_css_class("flat")
        close.add_css_class("circular")
        close.connect("clicked", lambda *_: on_close())
        box.append(close)
        reveal = (Gtk.StateFlags.PRELIGHT | Gtk.StateFlags.SELECTED
                  | Gtk.StateFlags.FOCUS_WITHIN)

        def sync(*_):
            close.set_opacity(1.0 if row.get_state_flags() & reveal else 0.0)

        row.connect("state-flags-changed", sync)
    row.set_child(box)
    return row, badge


def sidebar_placeholder(text):
    label = Gtk.Label(label=text, margin_top=24)
    label.add_css_class("dim-label")
    return label


def sidebar_action(popover, label, icon="digger-add-symbolic"):
    """Flat, left-aligned action button for the top of a sidebar."""
    btn = Gtk.MenuButton(popover=popover, margin_top=6, margin_bottom=2,
                         margin_start=6, margin_end=6)
    btn.set_child(Adw.ButtonContent(icon_name=icon, label=label,
                                    halign=Gtk.Align.START))
    btn.add_css_class("flat")
    return btn


def sidebar_button(label, icon, callback):
    """Like sidebar_action, but a plain button that runs a callback."""
    btn = Gtk.Button(margin_top=6, margin_bottom=2, margin_start=6, margin_end=6)
    btn.set_child(Adw.ButtonContent(icon_name=icon, label=label,
                                    halign=Gtk.Align.START))
    btn.add_css_class("flat")
    btn.connect("clicked", lambda *_: callback())
    return btn


def enable_row_toggle(listview):
    """Clicking anywhere on a tree row expands/collapses it (not just the arrow).
    Buttons inside the row and the arrow itself keep their own behaviour."""
    listview.set_single_click_activate(True)

    def activated(lv, position):
        row = lv.get_model().get_item(position)
        if row is not None and row.is_expandable():
            row.set_expanded(not row.get_expanded())

    listview.connect("activate", activated)


def pane_separator():
    """Separator under a pane header; dark (like the header bar's edge) in dark mode."""
    sep = Gtk.Separator()
    sep.add_css_class("pane-sep")
    return sep


def make_spinner(size=32):
    """Circular arc spinner (libadwaita >= 1.6); Gtk.Spinner on older versions."""
    if hasattr(Adw, "Spinner"):
        return Adw.Spinner(width_request=size, height_request=size,
                           halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    return Gtk.Spinner(spinning=True, width_request=size, height_request=size,
                       halign=Gtk.Align.CENTER)


class Placeholder(Gtk.Box):
    """Centered icon (or spinner) + title + description. Every empty/loading state
    in the right panel uses this, so size and placement are identical."""
    ICON_PX = 64

    def __init__(self, icon_name=None, title="", description=""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                         hexpand=True, vexpand=True, margin_top=24,
                         margin_bottom=24, margin_start=24, margin_end=24)
        self._slot = Gtk.Stack(halign=Gtk.Align.CENTER, width_request=self.ICON_PX,
                               height_request=self.ICON_PX)
        self._icon = Gtk.Image(pixel_size=self.ICON_PX)
        self._icon.add_css_class("dim-label")
        self._slot.add_named(self._icon, "icon")
        self._slot.add_named(make_spinner(48), "spinner")
        self._title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._title.add_css_class("title-2")
        self._desc = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                               max_width_chars=48)
        self._desc.add_css_class("dim-label")
        self._extra = None
        for w in (self._slot, self._title, self._desc):
            self.append(w)
        self.set_icon_name(icon_name)       # None -> spinner
        self.set_title(title)
        self.set_description(description)

    def set_icon_name(self, name):
        if name:
            self._icon.set_from_icon_name(name)
        self._slot.set_visible_child_name("icon" if name else "spinner")

    def set_title(self, text):
        self._title.set_text(text or "")

    def set_description(self, text):
        self._desc.set_text(text or "")
        self._desc.set_visible(bool(text))

    def set_child(self, widget):
        if self._extra is not None:
            self.remove(self._extra)
        self._extra = widget
        if widget is not None:
            self.append(widget)


def empty_page(icon, title, description=""):
    return Placeholder(icon, title, description)
