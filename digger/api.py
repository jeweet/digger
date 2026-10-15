"""Client for the slskd REST API (no GTK needed)."""

import base64
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .logger import log


# --------------------------------------------------------------------------
# API helper
# --------------------------------------------------------------------------
class Api:
    def __init__(self, base_url, token=None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.creds = None            # (username, password) for silent re-login
        self._lock = threading.Lock()
        self._down = False           # True while the server is unreachable

    def request(self, method, path, body=None, timeout=30, _retry=True, _dns=2,
                text=False, headers_out=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        tok = self.token
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if headers_out is not None:
                    headers_out.update({k.lower(): v for k, v in resp.headers.items()})
        except urllib.error.HTTPError as e:
            log.debug("%s %s -> HTTP %s", method, path, e.code)
            if (e.code == 401 and _retry and self.creds
                    and path != "/api/v0/session"):
                with self._lock:
                    if self.token == tok:  # nobody refreshed it meanwhile
                        log.info("Session expired, signing in again")
                        self.login(*self.creds)
                return self.request(method, path, body, timeout, _retry=False,
                                    text=text, headers_out=headers_out)
            raise
        except urllib.error.URLError as e:
            # A failed name lookup means nothing was sent, so it is always safe to
            # retry; local resolvers often drop a lookup under bursts of requests.
            if isinstance(e.reason, socket.gaierror) and _dns > 0:
                log.debug("%s %s: name lookup failed, retrying", method, path)
                time.sleep(0.4)
                return self.request(method, path, body, timeout, _retry, _dns - 1,
                                    text, headers_out)
            # many pollers run at once: warn once per outage, not once per request
            if not self._down:
                self._down = True
                log.warning("Cannot reach %s: %s (further failures are logged "
                            "at debug level until it recovers)", self.base, e.reason)
            else:
                log.debug("%s %s failed: %s", method, path, e.reason)
            raise
        if self._down:
            self._down = False
            log.info("Connection to %s restored", self.base)
        log.debug("%s %s -> ok", method, path)
        if text:                       # plain-text endpoints (YAML, validation)
            out = raw.decode("utf-8", "replace")
            try:
                parsed = json.loads(out)
                if isinstance(parsed, str):
                    return parsed
            except ValueError:
                pass
            return out
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def login(self, username, password):
        res = self.request("POST", "/api/v0/session",
                           {"username": username, "password": password},
                           _retry=False)
        self.token = res["token"]
        self.creds = (username, password)

    def application(self):
        return self.request("GET", "/api/v0/application", timeout=15)

    # users
    def user_info(self, username):
        u = urllib.parse.quote(username, safe="")
        return self.request("GET", f"/api/v0/users/{u}/info", timeout=20)

    def user_status(self, username):
        u = urllib.parse.quote(username, safe="")
        return self.request("GET", f"/api/v0/users/{u}/status", timeout=20)

    def user_browse_status(self, username):
        u = urllib.parse.quote(username, safe="")
        try:
            return self.request("GET", f"/api/v0/users/{u}/browse/status", timeout=10)
        except urllib.error.HTTPError as e:
            if e.code == 404:        # no browse in progress (yet)
                return None
            raise

    def download_position(self, username, tid):
        """Place in the remote user's queue for a queued download (or None)."""
        u = urllib.parse.quote(username, safe="")
        res = self.request("GET", f"/api/v0/transfers/downloads/{u}/{tid}/position",
                           timeout=20)
        if isinstance(res, dict):
            res = res.get("position", res.get("place"))
        return res if isinstance(res, int) else None

    def user_browse(self, username):
        u = urllib.parse.quote(username, safe="")
        return self.request("GET", f"/api/v0/users/{u}/browse", timeout=600)

    def start_search(self, text, search_id):
        return self.request("POST", "/api/v0/searches",
                            {"id": search_id, "searchText": text})

    def cancel_search(self, search_id):
        return self.request("PUT", f"/api/v0/searches/{search_id}")

    def search_state(self, search_id):
        return self.request("GET", f"/api/v0/searches/{search_id}")

    def search_responses(self, search_id):
        return self.request("GET", f"/api/v0/searches/{search_id}/responses") or []

    def download(self, username, files):
        """Queue files through the batches endpoint (the per-user one is obsolete).
        files: list of {"filename": ..., "size": ...}. Returns the failures list;
        raises only when nothing at all could be queued."""
        seen, unique = set(), []
        for f in files:                     # slskd rejects duplicate filenames
            if f.get("filename") not in seen:
                seen.add(f.get("filename"))
                unique.append({"filename": f.get("filename"), "size": f.get("size", 0)})
        for attempt in range(4):            # 429 = server is busy with another batch
            try:
                res = self.request("POST", "/api/v0/transfers/downloads/batches",
                                   {"username": username, "files": unique}, timeout=60)
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 3:
                    raise
                time.sleep(1.0 + attempt)
        failures = (res or {}).get("failures") or []
        for fl in failures:
            log.warning("Could not queue %s: %s", fl.get("filename"), fl.get("message"))
        if failures and len(failures) >= len(unique):
            raise RuntimeError(failures[0].get("message") or "Could not queue the files")
        return failures

    def transfers(self, kind, include_removed=False):
        """kind: 'downloads' or 'uploads'"""
        q = "?includeRemoved=true" if include_removed else ""
        return self.request("GET", f"/api/v0/transfers/{kind}{q}") or []

    # searches kept on the server (the same list the web UI shows)
    def searches(self):
        return self.request("GET", "/api/v0/searches") or []

    def delete_search(self, search_id):
        return self.request("DELETE", f"/api/v0/searches/{search_id}")

    def user_endpoint(self, username):
        """Network address of a user, 'ip:port' (None if offline/unknown)."""
        u = urllib.parse.quote(username, safe="")
        try:
            res = self.request("GET", f"/api/v0/users/{u}/endpoint", timeout=15)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        if isinstance(res, dict):
            addr, port = res.get("address"), res.get("port")
            return f"{addr}:{port}" if addr and port else (addr or None)
        return str(res) if res else None

    def cancel_transfer(self, kind, username, tid, remove=False):
        u = urllib.parse.quote(username, safe="")
        q = "?remove=true" if remove else ""
        return self.request("DELETE", f"/api/v0/transfers/{kind}/{u}/{tid}{q}")

    def clear_completed(self, kind):
        """Remove every completed transfer in one call."""
        return self.request("DELETE", f"/api/v0/transfers/{kind}/all/completed")

    # shares / server connection
    def rescan_shares(self):
        return self.request("PUT", "/api/v0/shares")

    def cancel_share_scan(self):
        return self.request("DELETE", "/api/v0/shares")

    def server_connect(self):
        return self.request("PUT", "/api/v0/server", timeout=60)

    def server_disconnect(self):
        return self.request("DELETE", "/api/v0/server", timeout=60)

    # chat (private conversations)
    def conversations(self):
        return self.request("GET", "/api/v0/conversations") or []

    def messages(self, username, unacked_only=False):
        u = urllib.parse.quote(username, safe="")
        q = "?unAcknowledgedOnly=true" if unacked_only else ""
        try:
            return self.request("GET", f"/api/v0/conversations/{u}/messages{q}") or []
        except urllib.error.HTTPError as e:
            if e.code == 404:  # conversation doesn't exist yet
                return []
            raise

    def send_message(self, username, text):
        u = urllib.parse.quote(username, safe="")
        return self.request("POST", f"/api/v0/conversations/{u}", text)

    def ack_conversation(self, username):
        u = urllib.parse.quote(username, safe="")
        return self.request("PUT", f"/api/v0/conversations/{u}")

    def close_conversation(self, username):
        u = urllib.parse.quote(username, safe="")
        return self.request("DELETE", f"/api/v0/conversations/{u}")

    # chat rooms
    def rooms_available(self):
        return self.request("GET", "/api/v0/rooms/available") or []

    def rooms_joined(self):
        return self.request("GET", "/api/v0/rooms/joined") or []

    def join_room(self, name):
        return self.request("POST", "/api/v0/rooms/joined", name)

    def leave_room(self, name):
        r = urllib.parse.quote(name, safe="")
        return self.request("DELETE", f"/api/v0/rooms/joined/{r}")

    def _room_get(self, name, tail):
        r = urllib.parse.quote(name, safe="")
        try:
            return self.request("GET", f"/api/v0/rooms/joined/{r}/{tail}") or []
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise

    def room_messages(self, name):
        return self._room_get(name, "messages")

    def add_room_member(self, name, username):
        r = urllib.parse.quote(name, safe="")
        return self.request("POST", f"/api/v0/rooms/joined/{r}/members", username)

    def room_tickers(self, name):
        """Topic tickers of a joined room as [(username, message)]."""
        r = urllib.parse.quote(name, safe="")
        try:
            res = self.request("GET", f"/api/v0/rooms/joined/{r}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise
        raw = (res or {}).get("tickers") if isinstance(res, dict) else None
        out = []
        for t in raw or []:
            if isinstance(t, dict) and t.get("username"):
                out.append((t["username"], t.get("message") or ""))
        return out

    def set_ticker(self, name, message):
        r = urllib.parse.quote(name, safe="")
        return self.request("POST", f"/api/v0/rooms/joined/{r}/ticker", message)

    def events(self, offset=0, limit=50):
        """Returns (events, total). Used as a cheap 'something happened' signal."""
        hdr = {}
        res = self.request("GET", f"/api/v0/events?offset={offset}&limit={limit}",
                           timeout=15, headers_out=hdr)
        try:
            total = int(hdr.get("x-total-count", "0"))
        except ValueError:
            total = 0
        return (res if isinstance(res, list) else []), total

    def user_directory(self, username, directory):
        """Full contents of one folder of a user's share: [{name, files:[...]}]."""
        u = urllib.parse.quote(username, safe="")
        return self.request("POST", f"/api/v0/users/{u}/directory",
                            {"directory": directory}, timeout=120) or []

    # files on the server (downloads / incomplete folders)
    @staticmethod
    def _b64(path):
        return urllib.parse.quote(base64.b64encode(path.encode()).decode(), safe="")

    def files_list(self, area, subdir=""):
        """area: 'downloads' or 'incomplete'; subdir is relative to that root."""
        tail = f"/{self._b64(subdir)}" if subdir else ""
        return self.request("GET", f"/api/v0/files/{area}/directories{tail}",
                            timeout=60) or {}

    def files_delete(self, area, rel_path, is_dir):
        kind = "directories" if is_dir else "files"
        return self.request("DELETE", f"/api/v0/files/{area}/{kind}/{self._b64(rel_path)}",
                            timeout=120)

    # logs (administrators only)
    def logs_live(self):
        return self.request("GET", "/api/v0/logs/live", timeout=30) or []

    def logs_files(self):
        return self.request("GET", "/api/v0/logs/files", timeout=30) or []

    def log_file_tail(self, name, nbytes=200_000):
        """Last part of a log file (a Range request keeps big logs cheap)."""
        req = urllib.request.Request(
            self.base + "/api/v0/logs/files/" + urllib.parse.quote(name, safe=""),
            headers={"Authorization": "Bearer " + (self.token or ""),
                     "Range": f"bytes=-{nbytes}", "Accept": "text/plain"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        return data[-nbytes:].decode("utf-8", "replace")

    # application control
    def check_latest_version(self):
        return self.request("GET", "/api/v0/application/version/latest?forceCheck=true",
                            timeout=60)

    def collect_garbage(self):
        return self.request("POST", "/api/v0/application/gc", timeout=60)

    def restart_server(self):
        return self.request("PUT", "/api/v0/application", timeout=15)

    def shutdown_server(self):
        return self.request("DELETE", "/api/v0/application", timeout=15)

    # server options
    def options(self):
        return self.request("GET", "/api/v0/options")

    def options_yaml(self):
        return self.request("GET", "/api/v0/options/yaml", text=True) or ""

    def validate_yaml(self, yaml_text):
        """Returns '' when valid, otherwise the server's error message."""
        return (self.request("POST", "/api/v0/options/yaml/validate", yaml_text,
                             text=True) or "").strip()

    def save_yaml(self, yaml_text):
        return self.request("PUT", "/api/v0/options/yaml", yaml_text, text=True)

    def shares_contents(self):
        """Directory listing of your own shares (same shape as a user browse)."""
        return self.request("GET", "/api/v0/shares/contents", timeout=600)

    def shares(self):
        return self.request("GET", "/api/v0/shares") or {}

    def room_users(self, name):
        return self._room_get(name, "users")

    def send_room_message(self, name, text):
        r = urllib.parse.quote(name, safe="")
        return self.request("POST", f"/api/v0/rooms/joined/{r}/messages", text)
