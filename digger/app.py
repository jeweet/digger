"""Application object and entry point."""

import logging
import os
import signal
import sys

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import constants
from .constants import APP_DIR, APP_ID, PAGE_SHORTCUTS
from .style import CSS
from .logger import log
from .compat import GLibUnix
from .icons import install_icons
from .window import MainWindow


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self._signals = 0
        self._bg_held = False
        self.main_window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(CSS)  # GTK >= 4.12
        except AttributeError:
            provider.load_from_data(CSS.encode())
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # digger.png lives next to this script
        # Installed (Flatpak) builds ship the icon under the application id;
        # a plain checkout uses digger.png next to the script.
        theme = Gtk.IconTheme.get_for_display(display)
        theme.add_search_path(APP_DIR)
        icon_root = install_icons()
        if icon_root:
            theme.add_search_path(icon_root)
        for candidate in (APP_ID, "digger"):
            if theme.has_icon(candidate):
                constants.ICON_NAME = candidate
                break
        Gtk.Window.set_default_icon_name(constants.ICON_NAME)

        # clicking a desktop notification opens the chat / room
        for name, handler in (("open-chat", self._on_open_chat),
                              ("open-room", self._on_open_room),
                              ("open-downloads", self._on_open_downloads)):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", handler)
            self.add_action(action)
        self.set_accels_for_action("win.quit", ["<Primary>q"])
        self.set_accels_for_action("win.settings", ["<Primary>comma"])
        self.set_accels_for_action("win.shortcuts", ["<Primary>question"])
        self.set_accels_for_action("win.new-search", ["<Primary>n"])
        self.set_accels_for_action("win.focus-filter", ["<Primary>f"])
        for i, page in enumerate(PAGE_SHORTCUTS, 1):
            self.set_accels_for_action("win.goto-" + page, ["<Alt>%d" % i])

    def do_activate(self):
        # also runs when the app is started again while it sits in the background
        if self.main_window is None:
            try:
                self.main_window = MainWindow(self)
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                print("Digger: could not create the main window", file=sys.stderr)
                self.quit()
                return
        self.leave_background()
        self.main_window.present()
        log.info("Window shown")

    def do_shutdown(self):
        if self.main_window is not None:
            self.main_window.shutdown()
        Adw.Application.do_shutdown(self)
        logging.shutdown()

    # -- background mode: hold the app open while the window is hidden --
    def enter_background(self):
        if not self._bg_held:
            self.hold()
            self._bg_held = True

    def leave_background(self):
        if self._bg_held:
            self.release()
            self._bg_held = False

    def _on_open_chat(self, _action, param):
        self.do_activate()
        self.main_window.show_chat(param.get_string())

    def _on_open_downloads(self, _action, _param):
        self.do_activate()
        self.main_window.viewstack.set_visible_child_name("downloads")

    def _on_open_room(self, _action, param):
        self.do_activate()
        self.main_window.show_room(param.get_string())

    # -- Ctrl+C / SIGTERM: quit through GTK instead of raising KeyboardInterrupt --
    def install_signal_handlers(self):
        if GLibUnix is not None and hasattr(GLibUnix, "signal_add"):
            add_signal = GLibUnix.signal_add
        elif hasattr(GLib, "unix_signal_add"):   # older GLib/PyGObject
            add_signal = GLib.unix_signal_add
        else:
            return
        # PyGObject re-raises KeyboardInterrupt after run() if Python's default
        # SIGINT handler is active; a non-default handler disables that fallback.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        for sig in (signal.SIGINT, signal.SIGTERM):
            add_signal(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)

    def _on_signal(self, sig):
        self._signals += 1
        if self._signals == 1:
            log.info("Received %s, quitting", signal.Signals(sig).name)
            self.quit()
        else:  # second Ctrl+C: don't wait for slow shutdown
            os._exit(128 + sig)
        return GLib.SOURCE_CONTINUE


def main():
    app = App()
    app.install_signal_handlers()
    return app.run(sys.argv)
