"""Paths, limits and other constants."""

import os

from gi.repository import GLib


APP_ID = "io.github.jeweet.Digger"


CONFIG_PATH = os.path.join(GLib.get_user_config_dir(), "digger.json")


# settings of the app before it was renamed; read once, then saved as digger.json
LEGACY_CONFIG_PATH = os.path.join(GLib.get_user_config_dir(), "slskd-client.json")


MAX_RESULTS = 20000   # raw files kept per search


MAX_SHOWN = 5000      # files shown after filtering


MAX_RECENTS = 30


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ICON_NAME = "digger"  # digger.png next to this script


ICON_PATHS = [os.path.join(APP_DIR, n + ".png")
              for n in ("io.github.jeweet.Digger", "digger")]


VERSION = "1.0.0"


LOG_DIR = os.path.join(GLib.get_user_state_dir(), "digger")


LOG_FILE = os.path.join(LOG_DIR, "digger.log")


LOG_LEVELS = [("Debug", "DEBUG"), ("Info", "INFO"), ("Warning", "WARNING"),
              ("Error", "ERROR")]


THEMES = [("system", "System"), ("light", "Light"), ("dark", "Dark")]


DEFAULT_CONFIG = {
    "url": "http://localhost:5030",
    "username": "",
    "recent_searches": [],
    "theme": "system",
    "log_level": "INFO",
    "stay_signed_in": True,
    "run_in_background": False,
    "notify_chat": False,
    "notify_rooms": False,
    "notify_rooms_mentions": True,
    "notify_downloads": False,
}


# page name -> menu title, in sidebar order (Alt+1…6 jump to them)
PAGE_SHORTCUTS = {"search": "Search", "downloads": "Downloads",
                  "uploads": "Uploads", "users": "Users", "chat": "Chat",
                  "rooms": "Rooms"}


ICON_SIZE = 24          # small enough to leave row heights unchanged


SORT_MODES = [
    ("best", "Best"),                    # free slot first, then fastest
    ("speed_desc", "Fastest"),
    ("speed_asc", "Slowest"),
    ("queue_asc", "Shortest queue"),
    ("queue_desc", "Longest queue"),
    ("files_desc", "Most files"),
]
