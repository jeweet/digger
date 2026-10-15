#!/usr/bin/env python3
"""A small fake slskd server for testing digger.py without a real server.

Run it standalone and point the client at it (handy for trying the UI):

    python3 tests/fake_slskd.py --port 5030        # login: slskd / slskd

The tests start it on a random port. Only the standard library is used.
The response shapes follow slskd's API as far as they are known; fields the real
server might name differently are the ones the client guesses at.
"""
import argparse
import base64
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

USERNAME, PASSWORD = "slskd", "slskd"
ME = "tester"                       # the Soulseek name (differs from the login)


def iso(offset=0):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + offset))


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.tokens = set()
        self.connected = True
        self.scanning = False
        self.remote_file_management = True
        self.remote_configuration = True
        self.yaml = "web:\n  port: 5030\n"
        self.searches = {}
        self.transfers = {"downloads": [], "uploads": []}
        self.events = []                        # oldest first
        self.conversations = {"alice": [{"id": "m1", "username": "alice",
                                         "message": "hi!", "direction": "In",
                                         "timestamp": iso(-60)}]}
        self.rooms = {"Lobby": {"messages": [], "users": [{"username": "alice",
                      "status": "Online"}, {"username": ME, "status": "Online"}],
                      "tickers": [{"username": "alice", "message": "hello world"}]}}
        self.offline = {"ghost"}
        self.files = {"downloads": {"Artist": {"Album": {"01.mp3": 1234, "02.mp3": 4321}},
                                    "loose.txt": 12}, "incomplete": {"part.tmp": 5}}
        self.log_lines = [{"timestamp": iso(-5), "level": "Information",
                           "message": "Started"}]
        self.requests = []                      # (method, path) log for assertions
        for i, (st, user) in enumerate([("Completed, Succeeded", "bob"),
                                        ("InProgress", "bob"),
                                        ("Queued, Remotely", "carol"),
                                        ("Completed, Errored", "carol")]):
            self.add_transfer("downloads", user, f"@@x\\Music\\Album\\t{i}.mp3", st)
        self.add_transfer("uploads", "dave", "@@y\\Share\\song.flac",
                          "Completed, Succeeded")

    def add_transfer(self, kind, user, filename, state, size=1000):
        t = {"id": str(uuid.uuid4()), "username": user, "filename": filename,
             "state": state, "size": size, "removed": False,
             "bytesTransferred": size if "Succeeded" in state else 100,
             "percentComplete": 100 if "Succeeded" in state else 10,
             "averageSpeed": 5000.0, "requestedAt": iso(-30)}
        self.transfers[kind].append(t)
        return t

    def add_event(self, type_, data):
        self.events.append({"id": str(uuid.uuid4()), "timestamp": iso(),
                            "type": type_, "data": json.dumps(data), "version": 0})

    def grouped(self, kind, include_removed):
        users = {}
        for t in self.transfers[kind]:
            if t["removed"] and not include_removed:
                continue
            d = t["filename"].rsplit("\\", 1)[0]
            users.setdefault(t["username"], {}).setdefault(d, []).append(t)
        return [{"username": u, "directories": [
            {"directory": d, "fileCount": len(f), "files": f} for d, f in dirs.items()]}
            for u, dirs in users.items()]


def make_handler(st):
    routes = []

    def route(method, pattern):
        def deco(fn):
            routes.append((method, re.compile("^" + pattern + "$"), fn))
            return fn
        return deco

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # -- plumbing --
        def send(self, code=200, body=None, ctype="application/json", headers=None):
            if body is None:
                payload = b""
            elif isinstance(body, bytes):
                payload = body
            elif ctype == "application/json":
                payload = json.dumps(body).encode()
            else:
                payload = str(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        def body_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw) if raw else None
            except ValueError:
                return None

        def handle_any(self, method):
            url = urlparse(self.path)
            path, self.q = url.path, parse_qs(url.query)
            with st.lock:
                st.requests.append((method, path))
            if not (method == "POST" and path == "/api/v0/session"):
                tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                if tok not in st.tokens:
                    return self.send(401)
            self.body = self.body_json()
            for m, rx, fn in routes:
                mt = rx.match(path)
                if m == method and mt:
                    with st.lock:
                        return fn(self, *[unquote(g) for g in mt.groups()])
            self.send(404)

        do_GET = lambda self: self.handle_any("GET")        # noqa: E731
        do_POST = lambda self: self.handle_any("POST")      # noqa: E731
        do_PUT = lambda self: self.handle_any("PUT")        # noqa: E731
        do_DELETE = lambda self: self.handle_any("DELETE")  # noqa: E731

    A = "/api/v0"

    # -- session / application --
    @route("POST", A + "/session")
    def login(h):
        b = h.body or {}
        if b.get("username") == USERNAME and b.get("password") == PASSWORD:
            tok = uuid.uuid4().hex
            st.tokens.add(tok)
            return h.send(200, {"token": tok, "name": USERNAME})
        h.send(401)

    @route("GET", A + "/application")
    def app(h):
        h.send(200, {"version": {"current": "0.22.0", "latest": "0.23.0",
                                 "isUpdateAvailable": True},
                     "server": {"isConnected": st.connected, "isLoggedIn": st.connected,
                                "address": "fake:2271", "state": "Connected"},
                     "user": {"username": ME},
                     "shares": {"files": 120, "directories": 10, "scanning": st.scanning},
                     "pendingShareRescan": False})

    @route("GET", A + "/application/version/latest")
    def latest(h):
        h.send(200, "0.23.0")

    @route("POST", A + "/application/gc")
    def gc(h):
        h.send(200)

    @route("PUT", A + "/application")
    def restart(h):
        h.send(204)

    @route("DELETE", A + "/application")
    def shutdown(h):
        h.send(204)

    @route("PUT", A + "/server")
    def connect(h):
        st.connected = True
        h.send(200)

    @route("DELETE", A + "/server")
    def disconnect(h):
        st.connected = False
        h.send(204)

    # -- shares --
    @route("GET", A + "/shares")
    def shares(h):
        h.send(200, {"local": [{"id": "s1", "alias": "Music", "localPath": "/music",
                                "remotePath": "Music", "isExcluded": False,
                                "directories": 10, "files": 120}]})

    @route("PUT", A + "/shares")
    def rescan(h):
        if st.scanning:
            return h.send(409)
        st.scanning = True
        h.send(200)

    @route("DELETE", A + "/shares")
    def cancel_scan(h):
        if not st.scanning:
            return h.send(404)
        st.scanning = False
        h.send(204)

    @route("GET", A + "/shares/contents")
    def share_contents(h):
        h.send(200, [{"name": "Music\\Mine", "fileCount": 1, "files": [
            {"filename": "own.mp3", "size": 99}]}])

    # -- options / logs / files --
    @route("GET", A + "/options")
    def options(h):
        h.send(200, {"web": {"port": 5030}, "soulseek": {"password": "*****"}})

    @route("GET", A + "/options/yaml")
    def yaml_get(h):
        if not st.remote_configuration:
            return h.send(403)
        h.send(200, st.yaml, "text/plain")

    @route("POST", A + "/options/yaml/validate")
    def yaml_validate(h):
        if not st.remote_configuration:
            return h.send(403)
        text = h.body if isinstance(h.body, str) else ""
        h.send(200, "bad: yaml" if "bad:" in text else "", "text/plain")

    @route("PUT", A + "/options/yaml")
    def yaml_put(h):
        if not st.remote_configuration:
            return h.send(403)
        st.yaml = h.body if isinstance(h.body, str) else st.yaml
        h.send(200)

    @route("GET", A + "/logs/live")
    def logs_live(h):
        h.send(200, st.log_lines)

    @route("GET", A + "/logs/files")
    def logs_files(h):
        h.send(200, [{"name": "slskd.log"}])

    @route("GET", A + "/logs/files/(.+)")
    def log_file(h, name):
        data = ("line\n" * 100).encode()
        rng = h.headers.get("Range")
        if rng and rng.startswith("bytes=-"):
            n = int(rng.split("-")[1])
            return h.send(206, data[-n:], "text/plain; charset=utf-8")
        h.send(200, data, "text/plain; charset=utf-8")

    def walk(area, rel):
        node = st.files[area]
        for part in [p for p in rel.split("/") if p]:
            node = node[part]
        return node

    def listing(area, rel):
        node = walk(area, rel)
        return {"name": rel or area, "fullName": rel or area,
                "directories": [{"name": k, "fullName": k} for k, v in node.items()
                                if isinstance(v, dict)],
                "files": [{"name": k, "fullName": k, "length": v}
                          for k, v in node.items() if not isinstance(v, dict)]}

    def decode(b64):
        return base64.b64decode(b64).decode()

    @route("GET", A + "/files/(downloads|incomplete)/directories")
    def files_root(h, area):
        h.send(200, listing(area, ""))

    @route("GET", A + "/files/(downloads|incomplete)/directories/(.+)")
    def files_sub(h, area, b64):
        try:
            h.send(200, listing(area, decode(b64)))
        except KeyError:
            h.send(404)

    @route("DELETE", A + "/files/(downloads|incomplete)/(files|directories)/(.+)")
    def files_delete(h, area, kind, b64):
        if not st.remote_file_management:
            return h.send(403)
        rel = decode(b64)
        parent, _s, leaf = rel.rpartition("/")
        try:
            del walk(area, parent)[leaf]
        except KeyError:
            return h.send(404)
        h.send(204)

    # -- events --
    @route("GET", A + "/events")
    def events(h):
        off = int(h.q.get("offset", ["0"])[0])
        lim = int(h.q.get("limit", ["100"])[0])
        newest_first = list(reversed(st.events))
        h.send(200, newest_first[off:off + lim],
               headers={"X-Total-Count": str(len(st.events))})

    # -- searches --
    def search_view(s, with_responses=False):
        done = s["cancelled"] or time.time() - s["started"] > 0.5
        out = {"id": s["id"], "searchText": s["text"], "isComplete": done,
               "state": "Completed, Cancelled" if s["cancelled"] else
               ("Completed, TimedOut" if done else "InProgress"),
               "startedAt": s["startedAt"], "fileCount": 2 if done else 0,
               "responseCount": 1 if done else 0, "lockedFileCount": 0}
        if with_responses:
            out["responses"] = responses(s) if done else []
        return out

    def responses(s):
        return [{"username": "bob", "token": 1, "freeUploadSlots": 1,
                 "uploadSpeed": 100000, "queueLength": 0, "fileCount": 2,
                 "files": [{"filename": f"Music\\{s['text']}\\a.mp3", "size": 100},
                           {"filename": f"Music\\{s['text']}\\b.mp3", "size": 200}]}]

    @route("POST", A + "/searches")
    def search_start(h):
        b = h.body or {}
        s = {"id": b.get("id") or str(uuid.uuid4()), "text": b.get("searchText", ""),
             "started": time.time(), "startedAt": iso(), "cancelled": False}
        st.searches[s["id"]] = s
        h.send(200, search_view(s))

    @route("GET", A + "/searches")
    def search_list(h):
        h.send(200, [search_view(s) for s in st.searches.values()])

    @route("GET", A + "/searches/([^/]+)")
    def search_get(h, sid):
        s = st.searches.get(sid)
        h.send(200, search_view(s)) if s else h.send(404)

    @route("GET", A + "/searches/([^/]+)/responses")
    def search_resp(h, sid):
        s = st.searches.get(sid)
        h.send(200, responses(s)) if s else h.send(404)

    @route("PUT", A + "/searches/([^/]+)")
    def search_cancel(h, sid):
        s = st.searches.get(sid)
        if not s:
            return h.send(404)
        s["cancelled"] = True
        h.send(200)

    @route("DELETE", A + "/searches/([^/]+)")
    def search_delete(h, sid):
        if st.searches.pop(sid, None) is None:
            return h.send(404)
        h.send(204)

    # -- transfers --
    @route("GET", A + "/transfers/(downloads|uploads)")
    def tx_list(h, kind):
        inc = h.q.get("includeRemoved", ["false"])[0] == "true"
        h.send(200, st.grouped(kind, inc))

    @route("POST", A + "/transfers/downloads/batches")
    def tx_batch(h):
        b = h.body or {}
        names = [f.get("filename") for f in b.get("files", [])]
        if len(names) != len(set(names)):
            return h.send(400, "duplicate filenames", "text/plain")
        if b.get("username") in st.offline:
            return h.send(404)
        failures, made = [], []
        for f in b["files"]:
            if "FAIL" in f["filename"]:
                failures.append({"filename": f["filename"], "message": "rejected"})
            else:
                made.append(st.add_transfer("downloads", b["username"], f["filename"],
                                            "Queued, Remotely", f.get("size", 0)))
        code = 201 if not failures else (207 if made else 200)
        h.send(code, {"batch": {"id": str(uuid.uuid4()), "transfers": made},
                      "failures": failures})

    @route("DELETE", A + "/transfers/(downloads|uploads)/all/completed")
    def tx_clear(h, kind):
        for t in st.transfers[kind]:
            if t["state"].startswith("Completed"):
                t["removed"] = True
        h.send(204)

    @route("DELETE", A + "/transfers/(downloads|uploads)/([^/]+)/([^/]+)")
    def tx_delete(h, kind, user, tid):
        for t in st.transfers[kind]:
            if t["id"] == tid:
                if h.q.get("remove", ["false"])[0] == "true" \
                        and t["state"].startswith("Completed"):
                    t["removed"] = True
                elif not t["state"].startswith("Completed"):
                    t["state"] = "Completed, Cancelled"
                return h.send(204)
        h.send(404)

    @route("GET", A + "/transfers/downloads/([^/]+)/([^/]+)/position")
    def tx_position(h, user, tid):
        h.send(200, {"position": 3})

    # -- users --
    def need_online(h, user):
        if user in st.offline:
            h.send(404, "offline", "text/plain")
            return False
        return True

    @route("GET", A + "/users/([^/]+)/info")
    def u_info(h, user):
        if need_online(h, user):
            h.send(200, {"description": "hi", "uploadSlots": 2, "queueLength": 0,
                         "hasFreeUploadSlot": True, "hasPicture": False})

    @route("GET", A + "/users/([^/]+)/status")
    def u_status(h, user):
        h.send(200, {"presence": "Online", "isPrivileged": False})

    @route("GET", A + "/users/([^/]+)/browse")
    def u_browse(h, user):
        if need_online(h, user):
            h.send(200, {"directories": [{"name": "Music\\Album", "fileCount": 1,
                                          "files": [{"filename": "x.mp3", "size": 5}]}]})

    @route("GET", A + "/users/([^/]+)/browse/status")
    def u_browse_status(h, user):
        h.send(404)

    @route("GET", A + "/users/([^/]+)/endpoint")
    def u_endpoint(h, user):
        if need_online(h, user):
            h.send(200, {"address": "203.0.113.5", "port": 50300})

    @route("POST", A + "/users/([^/]+)/directory")
    def u_directory(h, user):
        if not need_online(h, user):
            return
        d = (h.body or {}).get("directory")
        if not d:
            return h.send(400)
        h.send(200, [{"name": d, "fileCount": 2, "files": [
            {"filename": "one.mp3", "size": 1}, {"filename": "two.mp3", "size": 2}]}])

    # -- conversations --
    @route("GET", A + "/conversations")
    def conv_list(h):
        h.send(200, [{"username": u, "unAcknowledgedMessageCount": sum(
            1 for m in ms if not m.get("acked"))} for u, ms in st.conversations.items()])

    @route("GET", A + "/conversations/([^/]+)/messages")
    def conv_msgs(h, user):
        ms = st.conversations.get(user)
        h.send(200, ms) if ms is not None else h.send(404)

    @route("POST", A + "/conversations/([^/]+)")
    def conv_send(h, user):
        st.conversations.setdefault(user, []).append(
            {"id": uuid.uuid4().hex, "username": ME, "message": h.body,
             "direction": "Out", "timestamp": iso()})
        h.send(201)

    @route("PUT", A + "/conversations/([^/]+)")
    def conv_ack(h, user):
        for m in st.conversations.get(user, []):
            m["acked"] = True
        h.send(200)

    # -- rooms --
    @route("GET", A + "/rooms/available")
    def rooms_avail(h):
        h.send(200, [{"name": n, "userCount": 5} for n in ("Lobby", "Jazz")])

    @route("GET", A + "/rooms/joined")
    def rooms_joined(h):
        h.send(200, list(st.rooms))

    @route("POST", A + "/rooms/joined")
    def rooms_join(h):
        st.rooms.setdefault(h.body, {"messages": [], "users": [], "tickers": []})
        h.send(201, {"name": h.body})

    @route("GET", A + "/rooms/joined/([^/]+)")
    def room_get(h, name):
        r = st.rooms.get(name)
        # like the real API: users and messages only, no tickers
        h.send(200, {"name": name, "users": r["users"],
                     "messages": r["messages"]}) if r else h.send(404)

    @route("DELETE", A + "/rooms/joined/([^/]+)")
    def room_leave(h, name):
        h.send(204) if st.rooms.pop(name, None) is not None else h.send(404)

    @route("GET", A + "/rooms/joined/([^/]+)/messages")
    def room_msgs(h, name):
        r = st.rooms.get(name)
        h.send(200, r["messages"]) if r else h.send(404)

    @route("POST", A + "/rooms/joined/([^/]+)/messages")
    def room_send(h, name):
        r = st.rooms.get(name)
        if not r:
            return h.send(404)
        r["messages"].append({"username": ME, "message": h.body,
                              "timestamp": iso(), "self": True})
        h.send(201)

    @route("GET", A + "/rooms/joined/([^/]+)/users")
    def room_users(h, name):
        r = st.rooms.get(name)
        h.send(200, r["users"]) if r else h.send(404)

    @route("POST", A + "/rooms/joined/([^/]+)/ticker")
    def room_ticker(h, name):
        r = st.rooms.get(name)
        if not r:
            return h.send(404)
        r["tickers"] = [t for t in r["tickers"] if t["username"] != ME]
        r["tickers"].append({"username": ME, "message": h.body})
        h.send(201)

    @route("POST", A + "/rooms/joined/([^/]+)/members")
    def room_member(h, name):
        h.send(201) if name in st.rooms else h.send(404)

    return H


class FakeSlskd:
    """Context-manager wrapper: `with FakeSlskd() as srv:` -> srv.url, srv.state."""

    def __init__(self, port=0):
        self.state = State()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.state))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self):
        threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=5030)
    port = ap.parse_args().port
    srv = FakeSlskd(port).start()
    print(f"Fake slskd on {srv.url}  (login: {USERNAME} / {PASSWORD})")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
