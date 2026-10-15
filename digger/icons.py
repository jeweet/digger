"""Bundled symbolic icons and icon helpers."""

import base64
import json
import os
import zlib

from gi.repository import Gdk, Gio, GLib, Gtk

from . import constants
from .constants import ICON_SIZE
from .logger import log


# --------------------------------------------------------------------------
# Bundled symbolic icons. The system icon theme differs per desktop/phone (and
# sometimes lacks icons), so every symbolic icon the UI uses ships inside this
# file: the stock GNOME Adwaita symbolic icons (CC-BY-SA 3.0 / LGPL 3.0, GNOME
# Project), compressed. They are written to the user's cache dir at startup and
# exposed to GTK as "digger-*-symbolic", so they recolour like any symbolic icon.
# --------------------------------------------------------------------------
ICONS_DATA = (
    "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"
    "6wOoA1mKme7d01Nv+N+6VnXrbkf5z38B8vSbNA=="
)


def install_icons():
    """Write the bundled icons to the cache dir and return the theme root."""
    root = os.path.join(GLib.get_user_cache_dir(), "digger", "icons")
    dest = os.path.join(root, "hicolor", "scalable", "actions")
    try:
        icons = json.loads(zlib.decompress(base64.b64decode("".join(ICONS_DATA))))
        os.makedirs(dest, exist_ok=True)
        for name, svg in icons.items():
            path = os.path.join(dest, "digger-%s-symbolic.svg" % name)
            try:
                with open(path) as f:
                    if f.read() == svg:
                        continue
            except OSError:
                pass
            with open(path, "w") as f:
                f.write(svg)
    except (OSError, ValueError, zlib.error) as e:
        log.warning("Couldn't install bundled icons: %s", e)
        return None
    return root


_gicons = {}


def folder_gicon():
    if "/" not in _gicons:
        _gicons["/"] = Gio.ThemedIcon.new("folder")
    return _gicons["/"]


def file_gicon(name):
    """Themed icon for a file name, the same icons Nautilus shows per file type."""
    ext = os.path.splitext(name)[1].lower()
    icon = _gicons.get(ext)
    if icon is None:
        content_type, _uncertain = Gio.content_type_guess(name, None)
        icon = _gicons[ext] = Gio.content_type_get_icon(content_type)
    return icon


def set_item_icon(image, kind, name=""):
    """kind: 'dir' | 'file' | anything else (a user -> small symbolic avatar)."""
    if kind == "dir":
        image.set_from_gicon(folder_gicon())
        image.set_pixel_size(ICON_SIZE)
    elif kind == "file":
        image.set_from_gicon(file_gicon(name))
        image.set_pixel_size(ICON_SIZE)
    else:
        image.set_from_icon_name("digger-user-symbolic")
        image.set_pixel_size(16)


def has_app_icon():
    display = Gdk.Display.get_default()
    return bool(display) and Gtk.IconTheme.get_for_display(display).has_icon(constants.ICON_NAME)
