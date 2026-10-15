# Digger

A GTK 4 + libadwaita desktop client for [slskd](https://github.com/slskd/slskd), the
Soulseek daemon. It talks to the slskd REST API, so it works on the desktop and on
phones (postmarketOS / phosh).

    python3 run.py          # from a checkout
    python3 -m digger       # same thing

Needs Python 3, PyGObject, GTK 4 and libadwaita 1.4 or newer (1.5+ for the best
dialogs). libsecret is optional and stores the password in the system keyring.

## Layout

    digger/
      api.py            slskd REST client (no GTK)
      util.py           formatting and path helpers (no GTK)
      constants.py      app id, paths, limits
      config.py         settings file, saved password, theme, logging
      icons.py          bundled Adwaita symbolic icons
      style.py          CSS
      widgets.py        small reusable widgets
      messages.py       chat message rendering
      models.py         search / transfer / browse data models
      transfers.py      Downloads and Uploads view
      users.py          user info, user header, browse page, Users tab
      statusbar.py      bottom status bar
      window.py         main window: session, login, layout
      window_search.py  search tab        (mixin of the main window)
      window_chat.py    private chats     (mixin)
      window_rooms.py   rooms             (mixin)
      window_events.py  events feed and background polling (mixin)
      window_settings.py menu, settings and server settings (mixin)
      app.py            Gtk.Application and entry point
    tests/              unit tests with a fake slskd server (see tests/README.md)

The main window is one class assembled from five mixins, so each tab lives in its own
file while sharing `self`.

## Tests

    python3 -m unittest discover -s tests -v

They cover `digger/api.py` and `digger/util.py` and need no GTK.

## Packaging

Flatpak app id: `io.github.jeweet.Digger`. Icon: `io.github.jeweet.Digger.png`.
Settings: `~/.config/digger.json` (an older `slskd-client.json` is read once).
Log: `~/.local/state/digger/digger.log`.

The symbolic icons are the GNOME Adwaita icons (CC-BY-SA 3.0 / LGPL 3.0).

## Flatpak

    flatpak install flathub org.gnome.Platform//50 org.gnome.Sdk//50
    flatpak-builder --user --install --force-clean build-dir io.github.jeweet.Digger.yml
    flatpak run io.github.jeweet.Digger

Files: `io.github.jeweet.Digger.yml` (manifest), `data/io.github.jeweet.Digger.desktop`,
`data/io.github.jeweet.Digger.metainfo.xml`, `data/digger` (launcher), `io.github.jeweet.Digger.png` (512 px).

Before submitting to Flathub: check that the repository URLs in the metainfo
(`github.com/jeweet/digger`) exist, add at least one screenshot, and bump the runtime
version. The `io.github.jeweet.*` id is verified through the GitHub account `jeweet`.
