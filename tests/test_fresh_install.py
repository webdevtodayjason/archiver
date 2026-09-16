#!/usr/bin/env python3
"""The stranger's first minute.

Everything here runs against a database nobody has built anything in, because
that is the state every new install starts in and the one state the cockpit was
never opened against. 0.1.1 shipped a cockpit that queried `entity` on its two
opening routes and a database that only ever got the `entity` table if somebody
ran an entity extraction first, so the first page load of a fresh install failed
both of them.

Standard library only, and no subprocess anywhere: the farm's archive scanner
refuses a shipped tree that can start other programs, and the tests hold to the
same rule so they exercise the code the way it ships. The server under test runs
in a thread in this process.
"""
import contextlib
import io
import json
import os
import pathlib
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Read at import time by the modules below, so they have to be set before the
# first import rather than in setUp.
_TMP = tempfile.mkdtemp(prefix="brain-tests-")
os.environ["ARCHIVER_HOME"] = os.path.join(_TMP, "corpus")
os.environ["BRAIN_VAULT_CONFIG"] = os.path.join(_TMP, "tiiny-brain.json")
# A closed port on loopback, so the settings route's device probe fails at once
# instead of waiting out a timeout, and so a real device on the machine running
# these tests is never touched.
os.environ["TIINY_HOST"] = "127.0.0.1"
os.environ["TIINY_PORT"] = "9"
for _leak in ("TIINY_KEY", "TIINY_BASE", "FARM_DATA_DIR", "TIINY_DATA_DIR",
              "LASTLIGHT_CHAT_URL", "LASTLIGHT_CHAT_KEY"):
    os.environ.pop(_leak, None)

import archive    # noqa: E402
import cockpit    # noqa: E402

# Every table the app owns. The cockpit reads all of them and, before this, put
# none of them there.
TABLES = {"doc", "page", "chunk", "vec", "entity", "mention"}


def tearDownModule():  # noqa: N802
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)   # Windows may still hold the db open


def tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class Schema(unittest.TestCase):
    def test_a_fresh_database_gets_every_table(self):
        with tempfile.TemporaryDirectory(prefix="brain-schema-") as tmp:
            c = sqlite3.connect(os.path.join(tmp, "fresh.db"))
            try:
                archive.ensure_schema(c)
                names = tables(c)
            finally:
                c.close()
        self.assertIn("entity", names)     # the table the cockpit died on
        self.assertLessEqual(TABLES, names)

    def test_an_old_database_is_completed_in_place(self):
        """The half-built database every 0.1.1 install already has on disk."""
        with tempfile.TemporaryDirectory(prefix="brain-schema-") as tmp:
            path = os.path.join(tmp, "old.db")
            c = sqlite3.connect(path)
            try:
                c.executescript(archive.SCHEMA)     # doc, page, chunk, as 0.1.1 left it
                c.commit()
                self.assertNotIn("entity", tables(c))
                archive.ensure_schema(c)
                names = tables(c)
            finally:
                c.close()
        self.assertLessEqual(TABLES, names)

    def test_opening_the_database_the_ordinary_way_is_enough(self):
        c = archive.db()
        try:
            self.assertLessEqual(TABLES, tables(c))
        finally:
            c.close()

    def test_the_farm_data_directory_outlives_a_version_folder(self):
        """The farm replaces the version folder on update; data/ survives it."""
        with tempfile.TemporaryDirectory(prefix="brain-home-") as tmp:
            keep = dict(os.environ)
            try:
                os.environ.pop("ARCHIVER_HOME")
                os.environ["FARM_DATA_DIR"] = tmp
                self.assertEqual(pathlib.Path(tmp) / "corpus", archive._home())
                os.environ["ARCHIVER_HOME"] = tmp    # a named folder still wins
                self.assertEqual(pathlib.Path(tmp), archive._home())
            finally:
                os.environ.clear()
                os.environ.update(keep)


def ship_list():
    """The release script's file list, loaded from the script by path.

    The list has one home, scripts/build-release.py, and the hyphen in that name
    keeps it out of reach of a plain import.
    """
    import importlib.util
    path = ROOT / "scripts" / "build-release.py"
    spec = importlib.util.spec_from_file_location("build_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SHIPPED


class Shipped(unittest.TestCase):
    """What goes in the tarball, and the one rule about what may be in it."""

    def test_nothing_shipped_can_start_another_program(self):
        for name in ship_list():
            source = ROOT / name
            self.assertTrue(source.is_file(), f"{name} is on the ship list and not on disk")
            if source.suffix != ".py":
                continue
            text = source.read_text(encoding="utf-8")
            self.assertNotIn("import subprocess", text,
                             f"{name} would fail the farm's archive scanner")
            self.assertNotIn("os.system", text, name)

    def test_the_ship_list_carries_what_the_app_starts_with(self):
        shipped = ship_list()
        self.assertIn("cockpit.py", shipped)           # the entry point
        self.assertIn("static/cockpit.html", shipped)  # the page it serves
        for withheld in ("drivers.py", "pdfsource.py"):
            self.assertNotIn(withheld, shipped)


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class Cockpit(unittest.TestCase):
    """The two routes the page opens with, against an empty data directory."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), cockpit.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=10)

    def get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.status, r.read()

    def test_vitals_answers_json(self):
        code, body = self.get("/api/vitals")
        self.assertEqual(200, code)
        d = json.loads(body)
        self.assertEqual(0, d["notes"])
        self.assertEqual(0, d["entities"])
        self.assertEqual([], d["shelves"])

    def test_settings_answers_json(self):
        code, body = self.get("/api/settings")
        self.assertEqual(200, code)
        d = json.loads(body)
        self.assertEqual(0, d["corpus"]["entities"])
        self.assertFalse(d["device"]["reachable"])   # nothing is listening on :9

    def test_favicon_is_answered_not_missed(self):
        code, body = self.get("/favicon.ico")
        self.assertEqual(204, code)
        self.assertEqual(b"", body)

    def test_a_raising_route_answers_500_rather_than_closing_the_socket(self):
        """The regression that hid this bug for two releases."""
        def explode(c):
            raise RuntimeError("boom")

        kept, cockpit.vitals = cockpit.vitals, explode
        try:
            # The traceback belongs in farm.log, not in the test output.
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.get("/api/vitals")
        finally:
            cockpit.vitals = kept
        self.assertEqual(500, caught.exception.code)
        self.assertIn("boom", json.loads(caught.exception.read())["error"])


if __name__ == "__main__":
    unittest.main()
