"""Run work on a thread and deliver results to the GTK main loop."""

import threading

from gi.repository import GLib

from .logger import log


def deliver(cb, arg):
    cb(arg)
    return False  # run once; never let a truthy return re-schedule the idle


def run_async(fn, on_ok=None, on_err=None):
    """Run fn in a thread; deliver result/exception on the GTK main loop."""
    def worker():
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            log.debug("background task failed: %r", e)
            if on_err:
                GLib.idle_add(deliver, on_err, e)
            return
        if on_ok:
            GLib.idle_add(deliver, on_ok, result)

    threading.Thread(target=worker, daemon=True).start()
