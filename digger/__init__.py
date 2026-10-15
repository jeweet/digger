"""Digger: a GTK 4 + libadwaita client for the slskd REST API (Soulseek)."""
try:                                    # the API and helper modules also work without GTK
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except (ImportError, ValueError):
    pass
