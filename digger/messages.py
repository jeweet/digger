"""Chat message rendering shared by chats and rooms."""

import weakref
import zlib

from gi.repository import Adw, Gdk, GLib, Gtk

from .widgets import clear_children


def fmt_bubble_time(ts):
    try:
        dt = GLib.DateTime.new_from_iso8601(ts, None).to_local()
        today = GLib.DateTime.new_now_local()
        same_day = dt.format("%Y-%m-%d") == today.format("%Y-%m-%d")
        return dt.format("%H:%M" if same_day else "%b %e, %H:%M").strip()
    except Exception:  # noqa: BLE001
        return ts or ""


def make_message_view():
    """Scrolled, full-width message column shared by private chat and rooms."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                  margin_top=12, margin_bottom=12, margin_start=18, margin_end=18)
    scroll = Gtk.ScrolledWindow(vexpand=True, child=box,
                                hscrollbar_policy=Gtk.PolicyType.NEVER)
    return scroll, box


# Name colours for the plain-text chat layout: one stable colour per user,
# darker shades on a light theme and lighter ones on a dark theme.
NAME_COLORS_LIGHT = ["#c64600", "#1c71d8", "#26a269", "#a51d2d", "#813d9c",
                     "#865e3c", "#0e7490", "#b5179e", "#5c6bc0", "#6f8700"]


NAME_COLORS_DARK = ["#ffa348", "#78aeed", "#8ff0a4", "#f66151", "#dc8add",
                    "#cdab8f", "#5bd7e8", "#f08bdc", "#a5b4fc", "#c1d56a"]


def name_color(user):
    dark = Adw.StyleManager.get_default().get_dark()
    palette = NAME_COLORS_DARK if dark else NAME_COLORS_LIGHT
    return palette[zlib.crc32((user or "").lower().encode()) % len(palette)]


def _msg_epoch(ts):
    try:
        return GLib.DateTime.new_from_iso8601(ts, None).to_unix()
    except Exception:  # noqa: BLE001
        return 0


# Phone layout: message text is not selectable, so a finger drag always scrolls.
# Press and hold a message to copy it instead.
MSG_LABELS = weakref.WeakSet()


MSG_COMPACT = [False]


COPY_NOTIFY = [lambda _text: None]       # the main window shows a toast


def set_messages_compact(compact):
    MSG_COMPACT[0] = compact
    for label in list(MSG_LABELS):
        label.set_selectable(not compact)


def make_message_label(text, tooltip=None):
    label = Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=2,
                      selectable=not MSG_COMPACT[0], hexpand=True,
                      tooltip_text=tooltip)
    MSG_LABELS.add(label)

    def on_hold(_gesture, _x, _y):
        if not MSG_COMPACT[0]:
            return
        label.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))
        COPY_NOTIFY[0]("Message copied")

    hold = Gtk.GestureLongPress()
    hold.connect("pressed", on_hold)
    label.add_controller(hold)
    return label


def render_bubbles(box, items, show_author=True, mention=None):
    """items: [(user, timestamp, text, own)] -> a plain, left-aligned message log.

    Each run of messages from one sender gets a header (bold coloured name plus
    the time); the messages follow underneath as ordinary text. A new header also
    starts after a pause of more than five minutes. Messages matching the compiled
    regex `mention` (not your own) are highlighted."""
    clear_children(box)
    prev, prev_t = None, 0
    group = None
    for user, ts, text, own in items:
        key = (user, own)
        t = _msg_epoch(ts)
        if group is None or key != prev or (t and prev_t and t - prev_t > 300):
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                            margin_top=0 if not box.get_first_child() else 14)
            head = Gtk.Box(spacing=8)
            who = "You" if own else user
            name = Gtk.Label(xalign=0, ellipsize=3)
            if own:
                name.set_markup(f"<b>{GLib.markup_escape_text(who)}</b>")
                name.add_css_class("accent")
            else:
                name.set_markup(f'<span foreground="{name_color(user)}" '
                                f'weight="bold">{GLib.markup_escape_text(who)}</span>')
            when = Gtk.Label(label=fmt_bubble_time(ts), xalign=0)
            when.add_css_class("dim-label")
            when.add_css_class("caption")
            head.append(name)
            head.append(when)
            group.append(head)
            box.append(group)
        prev, prev_t = key, t or prev_t
        line = make_message_label(text, fmt_bubble_time(ts))
        if mention is not None and not own and mention.search(text or ""):
            line.add_css_class("msg-mention")
        group.append(line)


def make_compose_bar(on_send):
    bar = Gtk.Box(spacing=6, margin_top=8, margin_bottom=8,
                  margin_start=12, margin_end=12)
    entry = Gtk.Entry(hexpand=True, placeholder_text="Message…", sensitive=False)
    entry.connect("activate", on_send)
    send = Gtk.Button(icon_name="digger-send-symbolic", tooltip_text="Send",
                      valign=Gtk.Align.CENTER)
    send.add_css_class("suggested-action")
    send.add_css_class("circular")
    send.connect("clicked", on_send)
    bar.append(entry)
    bar.append(send)
    return bar, entry
