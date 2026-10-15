"""Optional libraries (libsecret, GLibUnix)."""

import gi


try:  # GLib.unix_signal_add is deprecated in favour of GLibUnix.signal_add
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix  # noqa: E402
except (ValueError, ImportError):
    GLibUnix = None


try:
    gi.require_version("Secret", "1")
    from gi.repository import Secret  # noqa: E402
    # schema name kept from before the rename, so saved passwords are still found
    SECRET_SCHEMA = Secret.Schema.new(
        "io.github.example.SlskdClient", Secret.SchemaFlags.NONE,
        {"server": Secret.SchemaAttributeType.STRING,
         "username": Secret.SchemaAttributeType.STRING})
except (ValueError, ImportError):
    Secret = None
    SECRET_SCHEMA = None
