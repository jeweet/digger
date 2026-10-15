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
    "kQNt"
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
