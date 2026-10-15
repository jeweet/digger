# Tests

No GTK needed. The tests load only the `Api` class and the plain helper functions
from `digger.py` and run them against a fake slskd server.

    python3 -m unittest discover -s tests -v

## Try the UI without a real server

    python3 tests/fake_slskd.py --port 5030
    python3 run.py            # URL http://127.0.0.1:5030, login slskd / slskd

The fake server has a few searches, transfers (including errored and queued ones),
a chat with `alice`, a room `Lobby`, a user `ghost` who is offline, files to
browse and delete, and server settings. It is a simplified imitation: where I
had to guess slskd's real field names, the fake uses the same guess, so passing
tests do not prove the guess is right.
