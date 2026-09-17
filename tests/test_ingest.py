#!/usr/bin/env python3
"""Loading notes from the page, on a folder nobody has loaded before.

The four things that have to hold, and each of them cost something to learn:

  The walk files a note under its folder and calls it by its filename, and the
  name it files it under does not depend on which operating system read the
  folder. A Windows walk hands back ``Projects\\note.md``; the same note loaded
  on a Mac has to be the same row, not a second one.

  Loading twice is cheap. A note already in and unchanged is not re-read, not
  re-chunked and not re-embedded, because re-embedding a vault is minutes of
  somebody's device time and they pressed the same button they pressed before.

  A step that cannot run says which step and why, and keeps what the earlier
  steps produced. The Tiiny holding no embedding model is the normal case here,
  not a fault.

  The two routes answer. The page is the only way in now, so a route that drops
  its socket is the whole feature gone.

Standard library only, no subprocess, and the embedding step is a stub: these
run in CI on three operating systems with no device anywhere near them.
"""
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="brain-ingest-")
os.environ["ARCHIVER_HOME"] = os.path.join(_TMP, "corpus")
os.environ["BRAIN_VAULT_CONFIG"] = os.path.join(_TMP, "tiiny-brain.json")
os.environ["TIINY_HOST"] = "127.0.0.1"
os.environ["TIINY_PORT"] = "9"          # closed, so nothing here touches a device
for _leak in ("TIINY_KEY", "TIINY_BASE", "FARM_DATA_DIR", "TIINY_DATA_DIR",
              "LASTLIGHT_CHAT_URL", "LASTLIGHT_CHAT_KEY"):
    os.environ.pop(_leak, None)
# A key that is set and worthless, because "configured" and "unconfigured" are
# two different answers here and most of these tests mean the first one. It is
# only ever sent to 127.0.0.1:9, which is closed.
os.environ["TIINY_KEY"] = "not-a-real-key"

import archive      # noqa: E402
import cockpit      # noqa: E402
import ingest       # noqa: E402
import md2jsonl     # noqa: E402

BODY = ("This note is long enough to be worth indexing, which takes rather more "
        "than a line of text. " * 3)


def tearDownModule():  # noqa: N802
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)


def a_vault(where, notes=None):
    """A folder shaped like somebody's vault: two projects and a loose note."""
    root = pathlib.Path(where)
    notes = notes or {
        "Projects/Gateway.md": "# Gateway\n\n" + BODY,
        "Projects/Ports.md": "# Ports\n\n" + BODY,
        "Reading/Hides.md": "# Hides\n\n" + BODY,
        "Inbox.md": "# Inbox\n\n" + BODY,
    }
    for rel, text in notes.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def wait_for_idle(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        d = ingest.state()
        if not d.get("running"):
            return d
        time.sleep(0.05)
    raise AssertionError("the loader never finished")


def fake_embed(dim=8):
    """An embedder that answers, so the state machine can be driven without one."""
    def embed(texts):
        return [[float((i + j) % 7) + 1.0 for j in range(dim)]
                for i, _t in enumerate(texts)]
    return embed


def refuses(texts):
    raise RuntimeError("model 'Qwen/Qwen3-Embedding-0.6B' is not loaded")


class Walk(unittest.TestCase):
    """The folder walk: what becomes a project, what becomes a title, what is
    passed over."""

    def test_folders_become_projects_and_filenames_become_titles(self):
        with tempfile.TemporaryDirectory(prefix="brain-walk-") as tmp:
            root = a_vault(tmp)
            (root / "Projects" / "diagram.png").write_bytes(b"not markdown")
            (root / "Projects" / "notes.txt").write_text("also not markdown")
            (root / ".obsidian").mkdir()
            (root / ".obsidian" / "workspace.md").write_text("# editor state\n\n" + BODY)
            got = {r["title"]: r for r, _rel in md2jsonl.records(root, ingest.MIN_CHARS)
                   if r}
        self.assertEqual({"Gateway", "Ports", "Hides", "Inbox"}, set(got))
        self.assertEqual("Projects", got["Gateway"]["shelf"])
        self.assertEqual("Reading", got["Hides"]["shelf"])
        self.assertEqual("(root)", got["Inbox"]["shelf"])          # a loose note
        self.assertNotIn("workspace", got)                         # .obsidian is skipped

    def test_a_note_with_nothing_in_it_is_counted_not_filed(self):
        with tempfile.TemporaryDirectory(prefix="brain-walk-") as tmp:
            root = a_vault(tmp, {"Real.md": BODY, "Stub.md": "# todo\n"})
            out = list(md2jsonl.records(root, ingest.MIN_CHARS))
        kept = [r for r, _ in out if r]
        short = [rel for r, rel in out if r is None]
        self.assertEqual(["Real"], [r["title"] for r in kept])
        self.assertEqual(1, len(short))

    def test_the_key_a_note_is_filed_under_is_the_same_on_every_os(self):
        """A Windows walk and a Mac walk must produce one row, not two."""
        win = pathlib.PureWindowsPath(r"Projects\Gateway.md")
        mac = pathlib.PurePosixPath("Projects/Gateway.md")
        self.assertEqual("Projects", md2jsonl.shelf_of(win))
        self.assertEqual(md2jsonl.shelf_of(mac), md2jsonl.shelf_of(win))
        self.assertEqual("Projects/Gateway.md", win.as_posix())
        self.assertEqual(mac.as_posix(), win.as_posix())

    def test_the_walk_carries_the_posix_path_whatever_the_os(self):
        with tempfile.TemporaryDirectory(prefix="brain-walk-") as tmp:
            root = a_vault(tmp)
            paths = sorted(r["path"] for r, _ in md2jsonl.records(root, ingest.MIN_CHARS)
                           if r)
        self.assertEqual(["Inbox.md", "Projects/Gateway.md", "Projects/Ports.md",
                          "Reading/Hides.md"], paths)
        for p in paths:
            self.assertNotIn("\\", p)


class Browse(unittest.TestCase):
    """The folder browser, which is what a person without a terminal clicks."""

    def test_it_lists_subfolders_and_counts_what_is_in_them(self):
        with tempfile.TemporaryDirectory(prefix="brain-browse-") as tmp:
            root = a_vault(tmp)
            (root / "Reading" / "manual.pdf").write_bytes(b"%PDF-1.4\n")
            d = ingest.folders(str(root))
        self.assertEqual(["Projects", "Reading"], [f["name"] for f in d["folders"]])
        self.assertEqual(4, d["md"])
        self.assertEqual(1, d["pdf"])
        self.assertTrue(d["parent"])
        self.assertNotIn("error", d)

    def test_a_windows_path_on_another_os_is_answered_not_raised(self):
        """C:\\Users\\x\\Obsidian Vault is a folder on Windows and a sentence
        anywhere else. Either way the browser answers."""
        d = ingest.folders(r"C:\Users\x\Obsidian Vault")
        self.assertIsInstance(d, dict)
        self.assertIn("shortcuts", d)
        if os.name == "nt":
            # On Windows it is a real path; it just does not exist on the runner.
            self.assertIn("error", d)
        else:
            self.assertIn("error", d)
            self.assertIn("not a folder", d["error"])

    def test_every_shortcut_it_offers_exists(self):
        for s in ingest.shortcuts():
            self.assertTrue(pathlib.Path(s["path"]).is_dir(), s)

    def test_an_empty_path_lands_somewhere_a_person_recognises(self):
        d = ingest.folders("")
        self.assertEqual(str(pathlib.Path.home()), d["path"])


class Loading(unittest.TestCase):
    """The state machine, with the embedding step stubbed."""

    def setUp(self):
        c = archive.db()
        for t in ("vec", "mention", "entity", "chunk", "page", "doc"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
        c.close()
        ingest._JOB.clear()
        ingest._JOB.update({"running": False, "state": "idle"})

    def counts(self):
        c = archive.db()
        try:
            q = lambda s: c.execute(s).fetchone()[0]
            return {"docs": q("SELECT COUNT(*) FROM doc"),
                    "chunks": q("SELECT COUNT(*) FROM chunk"),
                    "vecs": q("SELECT COUNT(*) FROM vec"),
                    "entities": q("SELECT COUNT(*) FROM entity")}
        finally:
            c.close()

    def test_a_folder_of_markdown_ends_up_on_the_board(self):
        with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
            root = a_vault(tmp)
            ingest.start(str(root), embed=fake_embed())
            d = wait_for_idle()
        self.assertEqual("done", d["state"], d.get("message"))
        self.assertEqual(4, d["added"])
        self.assertEqual(0, d["unchanged"])
        n = self.counts()
        self.assertEqual(4, n["docs"])
        self.assertTrue(n["chunks"])
        self.assertEqual(n["chunks"], n["vecs"])     # every passage got a vector
        self.assertEqual("done", d["stage"])

    def test_the_shelf_a_note_lands_on_is_its_folder(self):
        with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
            ingest.start(str(a_vault(tmp)), embed=fake_embed())
            wait_for_idle()
        c = archive.db()
        try:
            rows = dict(c.execute("SELECT title, source FROM doc").fetchall())
        finally:
            c.close()
        self.assertEqual("Projects", rows["Gateway"])
        self.assertEqual("(root)", rows["Inbox"])

    def test_loading_the_same_folder_again_does_nothing_twice(self):
        with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
            root = a_vault(tmp)
            ingest.start(str(root), embed=fake_embed())
            wait_for_idle()
            first = self.counts()

            seen = []

            def counting_embed(texts):
                seen.append(len(texts))
                return fake_embed()(texts)

            ingest.start(str(root), embed=counting_embed)
            d = wait_for_idle()
        self.assertEqual("done", d["state"], d.get("message"))
        self.assertEqual(4, d["unchanged"])
        self.assertEqual(0, d["added"])
        self.assertEqual([], seen, "a second load re-embedded notes that had not changed")
        self.assertEqual(first, self.counts())

    def test_a_note_edited_on_disk_is_read_again_and_only_that_note(self):
        with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
            root = a_vault(tmp)
            ingest.start(str(root), embed=fake_embed())
            wait_for_idle()
            before = self.counts()
            (root / "Projects" / "Gateway.md").write_text(
                "# Gateway\n\nThe gateway moved to port eighty. " + BODY,
                encoding="utf-8")
            ingest.start(str(root), embed=fake_embed())
            d = wait_for_idle()
        self.assertEqual("done", d["state"], d.get("message"))
        self.assertEqual(1, d["updated"])
        self.assertEqual(3, d["unchanged"])
        after = self.counts()
        self.assertEqual(before["docs"], after["docs"])          # no duplicate row
        self.assertEqual(after["chunks"], after["vecs"])         # no orphan vector

    def test_two_vaults_with_the_same_note_name_do_not_overwrite_each_other(self):
        """Work and personal both have an Inbox at the top. Filed on the
        relative path alone the second load replaces the first, and a note the
        person can still see on disk disappears off their board."""
        with tempfile.TemporaryDirectory(prefix="brain-work-") as a, \
             tempfile.TemporaryDirectory(prefix="brain-home-") as b:
            a_vault(a, {"Inbox.md": "# Inbox\n\nthe gateway moved. " + BODY})
            a_vault(b, {"Inbox.md": "# Inbox\n\ntanning a hide. " + BODY})
            ingest.start(a, embed=fake_embed())
            wait_for_idle()
            ingest.start(b, embed=fake_embed())
            d = wait_for_idle()
        self.assertEqual("done", d["state"], d.get("message"))
        self.assertEqual(1, d["added"])          # the second one is new, not a change
        self.assertEqual(0, d["updated"])
        self.assertEqual(2, self.counts()["docs"])

    def test_the_board_cache_is_dropped_on_the_copy_that_is_serving(self):
        """cockpit.py is started as a script, so the module serving the page is
        __main__ and `import cockpit` is a second copy with its own cache."""
        import types
        script = types.ModuleType("__main__")

        def graph(c):                 # what a script-started cockpit._graph is
            pass

        graph._cache = ("stale", {}, 0)
        script._graph = graph
        kept_main = sys.modules.get("__main__")
        sys.modules["__main__"] = script
        cockpit._graph._cache = ("stale", {}, 0)
        try:
            with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
                ingest.start(str(a_vault(tmp)), embed=fake_embed())
                d = wait_for_idle()
        finally:
            sys.modules["__main__"] = kept_main
        self.assertEqual("done", d["state"], d.get("message"))
        self.assertIsNone(graph._cache, "the serving copy kept its empty graph")
        self.assertIsNone(cockpit._graph._cache)

    def test_a_folder_with_no_markdown_says_so_and_stops(self):
        with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
            (pathlib.Path(tmp) / "holiday.jpg").write_bytes(b"\xff\xd8")
            ingest.start(tmp, embed=fake_embed())
            d = wait_for_idle()
        self.assertEqual("empty", d["state"])
        self.assertIn("no markdown files", d["message"])
        self.assertEqual(0, self.counts()["docs"])

    def test_a_folder_of_stubs_says_they_were_too_short(self):
        with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
            a_vault(tmp, {"One.md": "# a\n", "Two.md": "# b\n"})
            ingest.start(tmp, embed=fake_embed())
            d = wait_for_idle()
        self.assertEqual("too-short", d["state"])
        self.assertEqual(2, d["short"])

    def test_a_path_that_is_not_a_folder_says_so(self):
        ingest.start(os.path.join(_TMP, "nowhere-at-all"), embed=fake_embed())
        d = wait_for_idle()
        self.assertEqual("no-folder", d["state"])

    def test_no_embedding_model_keeps_the_notes_and_names_the_step(self):
        """The normal Tuesday: the Tiiny answers and serves no embedding model."""
        kept = ingest.models
        ingest.models = lambda: {"reachable": True, "base": "http://tiiny",
                                 "ids": ["Qwen/Qwen3-4B"]}
        try:
            with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
                ingest.start(str(a_vault(tmp)), embed=refuses)
                d = wait_for_idle()
        finally:
            ingest.models = kept
        self.assertEqual("model", d["state"])
        self.assertIn("no embedding model", d["message"])
        self.assertIn("Qwen/Qwen3-4B", d["message"])      # what it does serve
        self.assertEqual("names", d["stage"])             # it went on without it
        n = self.counts()
        self.assertEqual(4, n["docs"])                    # the reading is kept
        self.assertTrue(n["chunks"])
        self.assertEqual(0, n["vecs"])

    def test_try_again_after_a_model_is_loaded_finishes_the_job(self):
        kept = ingest.models
        ingest.models = lambda: {"reachable": True, "base": "http://tiiny", "ids": []}
        try:
            with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
                root = a_vault(tmp)
                ingest.start(str(root), embed=refuses)
                self.assertEqual("model", wait_for_idle()["state"])
                ingest.start(str(root), embed=fake_embed())   # the model came up
                d = wait_for_idle()
        finally:
            ingest.models = kept
        self.assertEqual("done", d["state"], d.get("message"))
        n = self.counts()
        self.assertEqual(n["chunks"], n["vecs"])

    def test_an_unreachable_device_is_a_different_answer_from_a_missing_model(self):
        with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
            ingest.start(str(a_vault(tmp)), embed=refuses)
            d = wait_for_idle()      # TIINY_PORT is 9, so nothing answers at all
        self.assertEqual("device", d["state"])
        self.assertIn("did not answer", d["message"])
        self.assertTrue(self.counts()["chunks"])          # still kept

    def test_a_device_nobody_has_set_up_says_to_set_one_up(self):
        """The state a genuinely fresh install lands in, and the one that used
        to hang the page: archivist.die() leaves by sys.exit, SystemExit is not
        an Exception, and the worker thread went with it holding running=True."""
        def unconfigured(texts):
            import archivist
            archivist.die("Set TIINY_HOST and TIINY_KEY.")

        import archivist
        kept = archivist.HOST, archivist.KEY
        archivist.HOST, archivist.KEY = "", ""
        try:
            with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
                ingest.start(str(a_vault(tmp)), embed=unconfigured)
                d = wait_for_idle(15)
        finally:
            archivist.HOST, archivist.KEY = kept
        self.assertFalse(d["running"])            # it must never be left running
        self.assertEqual("unset", d["state"])
        self.assertIn("no Tiiny is set up yet", d["message"])
        self.assertTrue(self.counts()["chunks"])  # and the reading is kept

    def test_a_step_that_blows_up_ends_the_job_rather_than_hanging_it(self):
        def explode(texts):
            raise KeyboardInterrupt("someone pressed something")

        with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
            ingest.start(str(a_vault(tmp)), embed=explode)
            d = wait_for_idle(15)
        self.assertFalse(d["running"])
        self.assertIn(d["state"], ("error", "device", "model", "unset"))

    def test_pdfs_are_counted_and_left_alone(self):
        with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
            root = a_vault(tmp)
            (root / "Reading" / "manual.pdf").write_bytes(b"%PDF-1.4\n")
            (root / "scan.pdf").write_bytes(b"%PDF-1.4\n")
            ingest.start(str(root), embed=fake_embed())
            d = wait_for_idle()
        self.assertEqual(2, d["pdfs"])
        self.assertEqual(4, d["added"])                   # the PDFs are not documents

    def test_two_loads_at_once_is_answered_not_run_twice(self):
        with tempfile.TemporaryDirectory(prefix="brain-load-") as tmp:
            root = a_vault(tmp)
            slow = threading.Event()

            def dawdle(texts):
                slow.wait(5)
                return fake_embed()(texts)

            ingest.start(str(root), embed=dawdle)
            for _ in range(200):          # until it is really in the slow step
                if ingest.state().get("stage") == "embedding":
                    break
                time.sleep(0.02)
            second = ingest.start(str(root), embed=fake_embed())
            self.assertTrue(second.get("busy"))
            self.assertIn("already", second.get("error", ""))
            slow.set()
            wait_for_idle()


def free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class Routes(unittest.TestCase):
    """The two routes the loader talks to."""

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

    def call(self, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_folders_answers_json_for_the_home_directory(self):
        code, d = self.call("/api/folders")
        self.assertEqual(200, code)
        self.assertEqual(str(pathlib.Path.home()), d["path"])
        self.assertIsInstance(d["folders"], list)
        self.assertIsInstance(d["shortcuts"], list)

    def test_folders_answers_a_windows_path_with_words_not_a_500(self):
        code, d = self.call("/api/folders?path=" +
                            urllib.parse.quote(r"C:\Users\x\Obsidian Vault"))
        self.assertEqual(200, code)
        self.assertIn("error", d)

    def test_the_polled_routes_answer_while_the_database_is_held(self):
        """The name index holds a write transaction for as long as it takes, and
        opening a connection runs the schema script. The two routes the page
        polls during a load must not be behind that."""
        c = archive.db()
        try:
            c.execute("BEGIN IMMEDIATE")         # a writer, the way the last step is
            t0 = time.time()
            code, d = self.call("/api/ingest")
            code2, _d2 = self.call("/api/folders")
            took = time.time() - t0
        finally:
            c.rollback()
            c.close()
        self.assertEqual(200, code)
        self.assertEqual(200, code2)
        self.assertIn("state", d)
        self.assertLess(took, 5, "the polled routes waited on the database")

    def test_ingest_reports_the_shape_the_page_polls(self):
        code, d = self.call("/api/ingest")
        self.assertEqual(200, code)
        for k in ("running", "stage", "done", "total", "errors", "started", "finished"):
            self.assertIn(k, d)

    def test_posting_no_folder_is_a_sentence_not_a_traceback(self):
        code, d = self.call("/api/ingest", {"path": ""})
        self.assertEqual(400, code)
        self.assertIn("folder", d["error"])

    def test_posting_a_folder_starts_it_and_remembers_it_as_the_vault(self):
        with tempfile.TemporaryDirectory(prefix="brain-route-") as tmp:
            root = a_vault(tmp)
            code, d = self.call("/api/ingest", {"path": str(root)})
            self.assertEqual(200, code)
            self.assertTrue(d["running"] or d["state"] != "idle")
            wait_for_idle()
            _code, v = self.call("/api/vault")
        self.assertEqual(str(root), v["vault"])


if __name__ == "__main__":
    unittest.main()
