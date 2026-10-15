#!/usr/bin/env python3
"""Tests for the parts of Digger that need no GTK: digger/api.py and the pure
helpers in digger/util.py. They run against tests/fake_slskd.py.

    python3 -m unittest discover -s tests -v
"""
import ast
import base64
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fake_slskd import FakeSlskd, PASSWORD, USERNAME  # noqa: E402

ROOT = os.path.join(HERE, "..")
WANTED_FUNCS = ["fmt_timespan", "fmt_speed", "tx_category", "tx_cat", "split_path",
                "basename", "build_browse_tree", "collect_files", "describe_error",
                "fmt_length"]


def load_client():
    """Import only the GTK-free modules (digger.api, digger.util). util asks for
    GLib.format_size, so a tiny stand-in for gi is installed first; that keeps the
    tests runnable on a machine without GTK."""
    stub = types.ModuleType("gi.repository")
    stub.GLib = types.SimpleNamespace(format_size=lambda n: f"{n} B")
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_a: None
    gi.repository = stub
    sys.modules["gi"], sys.modules["gi.repository"] = gi, stub
    sys.path.insert(0, ROOT)
    from digger import api, util
    ns = {"Api": api.Api}
    ns.update({name: getattr(util, name) for name in WANTED_FUNCS})
    return ns


NS = load_client()
Api = NS["Api"]


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.srv = FakeSlskd().start()
        self.addCleanup(self.srv.stop)
        self.st = self.srv.state
        self.api = Api(self.srv.url)
        self.api.login(USERNAME, PASSWORD)


class TestSession(ApiTestCase):
    def test_bad_password(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            Api(self.srv.url).login(USERNAME, "wrong")
        self.assertEqual(cm.exception.code, 401)

    def test_silent_relogin_after_token_expiry(self):
        self.st.tokens.clear()                 # server forgets our token
        app = self.api.application()
        self.assertEqual(app["user"]["username"], "tester")

    def test_dns_failure_is_retried(self):
        real = urllib.request.urlopen
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise urllib.error.URLError(socket.gaierror(-3, "no resolve"))
            return real(*a, **kw)

        with mock.patch("urllib.request.urlopen", flaky), \
                mock.patch.object(time, "sleep", lambda *_: None):
            self.assertIn("version", self.api.application())
        self.assertEqual(calls["n"], 3)


class TestTransfers(ApiTestCase):
    def files(self, include_removed=False, kind="downloads"):
        return [f for u in self.api.transfers(kind, include_removed)
                for d in u["directories"] for f in d["files"]]

    def test_list_groups_by_user_and_directory(self):
        users = {u["username"] for u in self.api.transfers("downloads")}
        self.assertEqual(users, {"bob", "carol"})

    def test_batch_download_dedupes_and_queues(self):
        before = len(self.files())
        failures = self.api.download("bob", [
            {"filename": "a\\x.mp3", "size": 1}, {"filename": "a\\x.mp3", "size": 1},
            {"filename": "a\\y.mp3", "size": 2}])
        self.assertEqual(failures, [])
        self.assertEqual(len(self.files()), before + 2)

    def test_batch_partial_failure_is_reported_not_raised(self):
        failures = self.api.download("bob", [{"filename": "ok.mp3", "size": 1},
                                             {"filename": "FAIL.mp3", "size": 1}])
        self.assertEqual(len(failures), 1)

    def test_batch_total_failure_raises(self):
        with self.assertRaises(RuntimeError):
            self.api.download("bob", [{"filename": "FAIL.mp3", "size": 1}])

    def test_batch_offline_user_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api.download("ghost", [{"filename": "a.mp3", "size": 1}])
        self.assertEqual(cm.exception.code, 404)

    def test_clear_completed_and_include_removed(self):
        done = [f for f in self.files() if f["state"].startswith("Completed")]
        self.assertTrue(done)
        self.api.clear_completed("downloads")
        self.assertFalse([f for f in self.files() if f["state"].startswith("Completed")])
        removed = [f for f in self.files(True) if f["removed"]]
        self.assertEqual(len(removed), len(done))

    def test_cancel_and_remove_single(self):
        active = next(f for f in self.files() if f["state"] == "InProgress")
        self.api.cancel_transfer("downloads", active["username"], active["id"])
        self.assertTrue(any(f["id"] == active["id"] and
                            f["state"] == "Completed, Cancelled" for f in self.files()))
        self.api.cancel_transfer("downloads", active["username"], active["id"],
                                 remove=True)
        self.assertFalse(any(f["id"] == active["id"] for f in self.files()))

    def test_queue_position(self):
        queued = next(f for f in self.files() if f["state"].startswith("Queued"))
        self.assertEqual(self.api.download_position(queued["username"], queued["id"]), 3)


class TestSearches(ApiTestCase):
    def test_search_lifecycle(self):
        self.api.start_search("hello", "sid-1")
        listed = self.api.searches()
        self.assertEqual([s["searchText"] for s in listed], ["hello"])
        time.sleep(0.6)
        self.assertTrue(self.api.search_state("sid-1")["isComplete"])
        self.assertEqual(len(self.api.search_responses("sid-1")), 1)
        self.api.delete_search("sid-1")
        self.assertEqual(self.api.searches(), [])

    def test_cancel(self):
        self.api.start_search("x", "sid-2")
        self.api.cancel_search("sid-2")
        self.assertTrue(self.api.search_state("sid-2")["isComplete"])


class TestUsers(ApiTestCase):
    def test_endpoint_formats(self):
        self.assertEqual(self.api.user_endpoint("bob"), "203.0.113.5:50300")
        self.assertIsNone(self.api.user_endpoint("ghost"))     # offline -> 404

    def test_browse_status_404_is_none(self):
        self.assertIsNone(self.api.user_browse_status("bob"))

    def test_browse_offline_raises_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api.user_browse("ghost")
        self.assertEqual(cm.exception.code, 404)

    def test_user_directory(self):
        res = self.api.user_directory("bob", "Music\\Album")
        self.assertEqual(len(res[0]["files"]), 2)

    def test_own_shares_contents_build_a_tree(self):
        tree = NS["build_browse_tree"](self.api.shares_contents())
        self.assertEqual(NS["collect_files"](tree)[0]["filename"], "own.mp3")


class TestServer(ApiTestCase):
    def test_connect_disconnect(self):
        self.api.server_disconnect()
        self.assertFalse(self.api.application()["server"]["isConnected"])
        self.api.server_connect()
        self.assertTrue(self.api.application()["server"]["isConnected"])

    def test_share_scan(self):
        self.api.rescan_shares()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api.rescan_shares()
        self.assertEqual(cm.exception.code, 409)
        self.api.cancel_share_scan()
        self.assertFalse(self.st.scanning)

    def test_yaml_roundtrip_and_validation(self):
        self.assertIn("port", self.api.options_yaml())
        self.assertEqual(self.api.validate_yaml("web:\n  port: 1\n"), "")
        self.assertNotEqual(self.api.validate_yaml("bad: yaml"), "")
        self.api.save_yaml("web:\n  port: 6000\n")
        self.assertIn("6000", self.api.options_yaml())

    def test_yaml_forbidden_when_remote_configuration_off(self):
        self.st.remote_configuration = False
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api.options_yaml()
        self.assertEqual(cm.exception.code, 403)

    def test_events_total_header_and_order(self):
        for i in range(3):
            self.st.add_event("RoomMessageReceived", {"roomName": f"r{i}"})
        events, total = self.api.events(0, 2)
        self.assertEqual((len(events), total), (2, 3))
        self.assertEqual(json.loads(events[0]["data"])["roomName"], "r2")  # newest first

    def test_log_tail_uses_range(self):
        tail = self.api.log_file_tail("slskd.log", nbytes=50)
        self.assertEqual(len(tail), 50)

    def test_application_actions(self):
        self.api.collect_garbage()
        self.api.check_latest_version()
        self.api.restart_server()


class TestFiles(ApiTestCase):
    def test_list_navigate_delete(self):
        root = self.api.files_list("downloads")
        self.assertEqual([d["name"] for d in root["directories"]], ["Artist"])
        sub = self.api.files_list("downloads", "Artist/Album")
        self.assertEqual({f["name"] for f in sub["files"]}, {"01.mp3", "02.mp3"})
        self.api.files_delete("downloads", "Artist/Album/01.mp3", is_dir=False)
        sub = self.api.files_list("downloads", "Artist/Album")
        self.assertEqual({f["name"] for f in sub["files"]}, {"02.mp3"})
        self.api.files_delete("downloads", "Artist", is_dir=True)
        self.assertEqual(self.api.files_list("downloads")["directories"], [])

    def test_delete_forbidden_without_remote_file_management(self):
        self.st.remote_file_management = False
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.api.files_delete("downloads", "loose.txt", is_dir=False)
        self.assertEqual(cm.exception.code, 403)


class TestChatAndRooms(ApiTestCase):
    def test_conversation_flow(self):
        self.assertEqual(self.api.conversations()[0]["unAcknowledgedMessageCount"], 1)
        self.api.send_message("alice", "yo")
        self.assertEqual(self.api.messages("alice")[-1]["message"], "yo")
        self.api.ack_conversation("alice")
        self.assertEqual(self.api.conversations()[0]["unAcknowledgedMessageCount"], 0)
        self.assertEqual(self.api.messages("nobody"), [])      # 404 -> empty

    def test_rooms_tickers_members(self):
        self.api.set_ticker("Lobby", "my ticker")
        self.assertEqual(self.st.rooms["Lobby"]["tickers"][-1]["message"], "my ticker")
        # slskd does not return tickers to clients, so reading yields nothing
        self.assertEqual(self.api.room_tickers("Lobby"), [])
        self.assertEqual(self.api.room_tickers("Nope"), [])
        self.api.add_room_member("Lobby", "carol")
        self.api.send_room_message("Lobby", "hey")
        self.assertEqual(self.api.room_messages("Lobby")[-1]["message"], "hey")
        self.api.leave_room("Lobby")
        self.assertEqual(self.api.rooms_joined(), [])


class TestHelpers(unittest.TestCase):
    def test_timespan(self):
        f = NS["fmt_timespan"]
        self.assertEqual(f("00:00:12.3456789"), "0:00:12")
        self.assertEqual(f("1.02:03:04"), "1d 2:03:04")
        self.assertEqual(f(None), "")

    def test_tx_category(self):
        c = NS["tx_category"]
        self.assertEqual(c("InProgress"), "active")
        self.assertEqual(c("Queued, Remotely"), "queued")
        self.assertEqual(c("Completed, Succeeded"), "completed")
        self.assertEqual(c("Completed, Cancelled"), "cancelled")
        self.assertEqual(c("Completed, TimedOut"), "errored")

    def test_removed_is_its_own_category(self):
        self.assertEqual(NS["tx_cat"]({"state": "InProgress", "removed": True}),
                         "removed")

    def test_split_path_and_basename(self):
        self.assertEqual(NS["split_path"]("a\\b\\c.mp3"), ("a\\b", "c.mp3"))
        self.assertEqual(NS["basename"]("a/b/c.mp3"), "c.mp3")

    def test_browse_tree_aggregates(self):
        tree = NS["build_browse_tree"]([
            {"name": "A\\B", "files": [{"filename": "x.mp3", "size": 3}]},
            {"name": "A", "files": [{"filename": "y.mp3", "size": 4}]}])
        self.assertEqual((tree["count"], tree["size"]), (2, 7))
        self.assertEqual(tree["dirs"]["A"]["dirs"]["B"]["count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
