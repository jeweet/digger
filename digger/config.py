"""Settings file, saved password, logging setup and theme."""

from logging.handlers import RotatingFileHandler
import json
import logging
import os
import threading

from gi.repository import Adw, Gio

from .constants import (
    APP_ID, CONFIG_PATH, DEFAULT_CONFIG, ICON_PATHS, LEGACY_CONFIG_PATH, LOG_DIR,
    LOG_FILE,
)
from .logger import log
from .compat import SECRET_SCHEMA, Secret


def setup_logging(level_name):
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            fh = RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=2,
                                     encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError:
            pass
    log.setLevel(getattr(logging, str(level_name).upper(), logging.INFO))


def apply_theme(name):
    scheme = {"light": Adw.ColorScheme.FORCE_LIGHT,
              "dark": Adw.ColorScheme.FORCE_DARK}.get(name, Adw.ColorScheme.DEFAULT)
    Adw.StyleManager.get_default().set_color_scheme(scheme)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    for path in (CONFIG_PATH, LEGACY_CONFIG_PATH):
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
            break
        except (OSError, json.JSONDecodeError):
            continue
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:
        log.warning("Could not save config: %s", e)


def _secret_attrs(url, user):
    return {"server": url.rstrip("/"), "username": user}


KEYRING_TIMEOUT = 8      # seconds; a missing or locked keyring must not hang the app


def _cancel_after(seconds):
    """A Gio.Cancellable that cancels itself, so a blocked keyring call gives up."""
    cancellable = Gio.Cancellable()
    timer = threading.Timer(seconds, cancellable.cancel)
    timer.daemon = True
    timer.start()
    return cancellable, timer


def store_password(cfg, url, user, pw):
    """Keyring if available, otherwise the (0600) config file."""
    if Secret:
        cancellable, timer = _cancel_after(KEYRING_TIMEOUT)
        try:
            if Secret.password_store_sync(
                    SECRET_SCHEMA, _secret_attrs(url, user),
                    Secret.COLLECTION_DEFAULT, f"Digger: {user} @ {url}",
                    pw, cancellable):
                cfg.pop("password", None)
                return "keyring"
        except Exception as e:  # noqa: BLE001
            log.warning("Keyring unavailable (%s); using config file", e)
        finally:
            timer.cancel()
    cfg["password"] = pw
    return "config file"


def load_password(cfg, url, user):
    if Secret:
        cancellable, timer = _cancel_after(KEYRING_TIMEOUT)
        try:
            pw = Secret.password_lookup_sync(SECRET_SCHEMA,
                                             _secret_attrs(url, user), cancellable)
            if pw:
                return pw
        except Exception as e:  # noqa: BLE001
            log.warning("Keyring lookup failed: %s", e)
        finally:
            timer.cancel()
    return cfg.get("password") or None


def forget_password(cfg, url, user):
    cfg.pop("password", None)
    if Secret and url and user:
        cancellable, timer = _cancel_after(KEYRING_TIMEOUT)
        try:
            Secret.password_clear_sync(SECRET_SCHEMA, _secret_attrs(url, user),
                                       cancellable)
        except Exception as e:  # noqa: BLE001
            log.debug("Keyring clear failed: %s", e)
        finally:
            timer.cancel()


def notification_icon():
    if os.path.exists("/.flatpak-info"):   # exported icon, resolved by the shell
        return Gio.ThemedIcon.new(APP_ID)
    for path in ICON_PATHS:                 # the shell can't see our search path
        if os.path.exists(path):
            return Gio.FileIcon.new(Gio.File.new_for_path(path))
    return None
