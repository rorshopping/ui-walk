"""Unit tests for embeddings.py, compare_runs.py and the sheet dedup path.

NO network and NO real embedding server: every endpoint call goes through
embeddings._post, which these tests replace with a fake, and the server
lifecycle tests probe only an in-process 127.0.0.1 TCP listener while
faking the spawn. Run:

  python -m unittest test_embeddings -v     # from scripts/
  python -m unittest discover -s scripts    # from the repo root
"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import compare_runs
import contact_sheets
import embeddings


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)
    return path


class CosineTest(unittest.TestCase):
    def test_identical(self):
        self.assertAlmostEqual(embeddings.cosine([1, 2, 3], [1, 2, 3]), 1.0)

    def test_orthogonal(self):
        self.assertAlmostEqual(embeddings.cosine([1, 0], [0, 1]), 0.0)

    def test_opposite(self):
        self.assertAlmostEqual(embeddings.cosine([1, 0], [-1, 0]), -1.0)

    def test_zero_vector_is_safe(self):
        self.assertEqual(embeddings.cosine([0, 0], [1, 2]), 0.0)
        self.assertEqual(embeddings.cosine([1, 2], [0, 0]), 0.0)
        self.assertEqual(embeddings.cosine([0, 0], [0, 0]), 0.0)


class FakePost:
    """Stands in for embeddings._post; one deterministic vec per data URI."""

    def __init__(self, vecs, error=None):
        self.vecs = vecs
        self.error = error
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return {"data": [{"embedding": list(self.vecs[
            entry["image_url"]["url"]])} for entry in payload["input"]]}


class EmbedCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = _write(os.path.join(self.tmp.name, "a.png"), b"A" * 32)
        self.b = _write(os.path.join(self.tmp.name, "b.png"), b"B" * 32)
        self.fake = FakePost({
            embeddings._data_uri(self.a): [1.0, 0.0, 0.0],
            embeddings._data_uri(self.b): [0.0, 1.0, 0.0],
        })
        self._orig_post = embeddings._post
        embeddings._post = self.fake
        self.addCleanup(setattr, embeddings, "_post", self._orig_post)

    def test_embeds_and_caches(self):
        vecs = embeddings.embed_files([self.a, self.b],
                                      cache_dir=self.tmp.name)
        self.assertEqual(vecs[os.path.abspath(self.a)], [1.0, 0.0, 0.0])
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(len(self.fake.calls[0]["input"]), 2)
        body = self.fake.calls[0]
        self.assertEqual(body["model"], embeddings.embed_model())
        self.assertTrue(all(p["type"] == "image_url" for p in body["input"]))
        # one JSON line per file with file/vec/mtime
        with open(os.path.join(self.tmp.name, "embeddings.jsonl")) as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(sorted(lines[0]), ["file", "mtime", "vec"])

    def test_cache_hit_skips_post(self):
        embeddings.embed_files([self.a, self.b], cache_dir=self.tmp.name)
        embeddings.embed_files([self.a, self.b], cache_dir=self.tmp.name)
        self.assertEqual(len(self.fake.calls), 1)

    def test_mtime_change_reembeds_only_stale_file(self):
        embeddings.embed_files([self.a, self.b], cache_dir=self.tmp.name)
        st = os.stat(self.a)
        os.utime(self.a, (st.st_atime, st.st_mtime + 500))
        embeddings.embed_files([self.a, self.b], cache_dir=self.tmp.name)
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(len(self.fake.calls[1]["input"]), 1)
        self.assertEqual(self.fake.calls[1]["input"][0]["image_url"]["url"],
                         embeddings._data_uri(self.a))

    def test_identical_files_embedded_once(self):
        x = _write(os.path.join(self.tmp.name, "x.png"), b"SAME" * 8)
        y = _write(os.path.join(self.tmp.name, "y.png"), b"SAME" * 8)
        self.fake.vecs[embeddings._data_uri(x)] = [0.5, 0.5, 0.5]
        vecs = embeddings.embed_files([x, y, self.a], cache_dir=self.tmp.name)
        # only the two distinct payloads hit the fake endpoint
        self.assertEqual(len(self.fake.calls[0]["input"]), 2)
        self.assertEqual(vecs[os.path.abspath(x)], vecs[os.path.abspath(y)])

    def test_empty_paths(self):
        self.assertEqual(embeddings.embed_files([], cache_dir=self.tmp.name),
                         {})


class HashDedupFallbackTest(unittest.TestCase):
    """--dedup must still drop byte-identical shots with NO server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.f1 = _write(os.path.join(d, "001_one.png"), b"P1" * 16)
        self.f2 = _write(os.path.join(d, "002_two.png"), b"P1" * 16)  # same
        self.f3 = _write(os.path.join(d, "003_three.png"), b"P3" * 16)
        self.files = [self.f1, self.f2, self.f3]
        self.orig = {f: i + 1 for i, f in enumerate(self.files)}
        self._orig_post = embeddings._post
        embeddings._post = FakePost({}, error=embeddings.EmbedUnavailable(
            "server down"))
        self.addCleanup(setattr, embeddings, "_post", self._orig_post)

    def test_exact_dups_dropped_without_server(self):
        kept, dups = contact_sheets.dedupe(self.files, 0.985,
                                           self.tmp.name, self.orig)
        self.assertEqual(kept, [self.f1, self.f3])
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["file"], self.f2)
        self.assertEqual(dups[0]["dup_of"], self.f1)
        self.assertIsNone(dups[0]["score"])
        self.assertEqual(contact_sheets.dup_note(dups[0], self.orig),
                         "dup of 001 (identical)")

    def test_file_hashes_distinguishes_content(self):
        hashes = embeddings.file_hashes(self.files)
        self.assertEqual(hashes[self.f1], hashes[self.f2])
        self.assertNotEqual(hashes[self.f1], hashes[self.f3])

    def test_note_format_for_scored_dup(self):
        note = contact_sheets.dup_note(
            {"dup_of": self.f2, "score": 0.9904}, self.orig)
        self.assertEqual(note, "dup of 002 (0.99)")


class PairingTest(unittest.TestCase):
    def setUp(self):
        self.keys_a = ["old/001.png", "old/002.png", "old/003.png"]
        e1, e2, e3 = [1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]
        self.vecs_a = dict(zip(self.keys_a, [e1, e2, e3]))
        self.keys_b = ["new/001.png", "new/002.png", "new/004.png"]
        # b1 ~ a1 (0.994), b2 == a2, b3 drifted from a2 (0.954)
        self.vecs_b = {"new/001.png": [0.9, 0.1],
                       "new/002.png": [0.0, 1.0],
                       "new/004.png": [0.3, 0.95]}

    def test_nearest_neighbor_pairing(self):
        pairs, unpaired_a = compare_runs.pair_shots(
            self.keys_a, self.vecs_a, self.keys_b, self.vecs_b)
        self.assertEqual([(p[0], p[1]) for p in pairs],
                         [(0, 0), (1, 1), (2, 1)])
        self.assertAlmostEqual(pairs[0][2], 0.99388, places=4)
        self.assertEqual(unpaired_a, [2])  # old/003.png chosen by nobody

    def test_classify_threshold(self):
        pairs, _ = compare_runs.pair_shots(self.keys_a, self.vecs_a,
                                           self.keys_b, self.vecs_b)
        unchanged, changed = compare_runs.classify(
            pairs, self.keys_a, self.keys_b, 0.97)
        self.assertEqual([r["a"] for r in unchanged], ["001.png", "002.png"])
        self.assertEqual(changed, [{"a": "002.png", "b": "004.png",
                                    "score": 0.9536}])

    def test_empty_baseline_pairs_nothing(self):
        pairs, unpaired_a = compare_runs.pair_shots(
            [], {}, self.keys_b, self.vecs_b)
        self.assertEqual(pairs, [])
        self.assertEqual(unpaired_a, [])

    def test_natural_shot_order(self):
        d = tempfile.mkdtemp()
        for name in ("010_late.png", "002_early.png", "001_first.png"):
            _write(os.path.join(d, name), b"x")
        self.assertEqual([os.path.basename(p) for p in compare_runs.shots(d)],
                         ["001_first.png", "002_early.png", "010_late.png"])


class FakeProc:
    """Minimal Popen stand-in: records terminate/wait, spawns nothing."""

    def __init__(self):
        self.terminated = False
        self.killed = False
        self.waits = 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waits += 1
        return 0


class ServerLifecycleTest(unittest.TestCase):
    """ensure_server / stop_server / embedding_server.

    Endpoint probing is REAL (an in-process 127.0.0.1 TCP listener); the
    spawn is FAKED via embeddings.subprocess.Popen — no llama-server and
    no long polls (tiny timeouts throughout).
    """

    def setUp(self):
        self._orig_url = os.environ.get("UIWALK_EMBED_URL")
        self._orig_popen = embeddings.subprocess.Popen
        self.listeners = []
        self.addCleanup(self._restore)

    def _restore(self):
        if self._orig_url is None:
            os.environ.pop("UIWALK_EMBED_URL", None)
        else:
            os.environ["UIWALK_EMBED_URL"] = self._orig_url
        embeddings.subprocess.Popen = self._orig_popen
        for s in self.listeners:
            s.close()

    def _free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # released: connections are refused until someone listens
        return port

    def _listen(self, port):
        s = socket.socket()
        s.bind(("127.0.0.1", port))
        s.listen(16)
        # a probe that connects and hangs up still occupies the backlog;
        # without a drain, Windows starts refusing after a few probes
        def drain():
            try:
                while True:
                    conn, _ = s.accept()
                    conn.close()
            except OSError:
                pass  # listener closed in cleanup — done
        threading.Thread(target=drain, daemon=True).start()
        self.listeners.append(s)
        return s

    def _point_url(self, port):
        os.environ["UIWALK_EMBED_URL"] = f"http://127.0.0.1:{port}/v1/embeddings"

    def _install_popen(self, on_spawn=None):
        """Swap in a fake Popen; returns (recorded calls, the proc it yields)."""
        calls = []
        proc = FakeProc()

        def fake_popen(cmd, **kwargs):
            calls.append((list(cmd), kwargs))
            if on_spawn is not None:
                on_spawn(cmd, kwargs)
            return proc

        embeddings.subprocess.Popen = fake_popen
        return calls, proc

    def test_already_running_returns_none_and_never_spawns(self):
        port = self._free_port()
        self._listen(port)  # real listener: the endpoint answers (TCP-wise)
        self._point_url(port)
        calls, _ = self._install_popen()
        self.assertIsNone(embeddings.ensure_server(timeout=2))
        self.assertEqual(calls, [])  # "already running — don't touch it"
        with embeddings.embedding_server(timeout=2) as proc:
            self.assertIsNone(proc)
        self.assertEqual(calls, [])

    def test_spawns_when_down_and_reports_ready(self):
        port = self._free_port()  # nothing listening: ensure_server spawns
        self._point_url(port)
        calls, proc = self._install_popen(
            on_spawn=lambda cmd, kw:
                self._listen(int(cmd[cmd.index("--port") + 1])))
        got = embeddings.ensure_server(timeout=10)
        self.assertIs(got, proc)
        cmd, kwargs = calls[0]
        self.assertEqual(cmd[:2], ["llama-server", "-hf"])
        self.assertEqual(cmd[3:], ["--embeddings", "--port", str(port)])
        if os.name == "nt":
            self.assertEqual(kwargs.get("creationflags"), 0x08000000)

    def test_context_manager_stops_what_it_started(self):
        port = self._free_port()
        self._point_url(port)
        calls, proc = self._install_popen(
            on_spawn=lambda cmd, kw:
                self._listen(int(cmd[cmd.index("--port") + 1])))
        with embeddings.embedding_server(timeout=10) as started:
            self.assertIs(started, proc)  # yields only what it started
            self.assertEqual(len(calls), 1)
        self.assertTrue(proc.terminated)  # stopped exactly once on exit
        self.assertEqual(proc.waits, 1)

    def test_context_manager_leaves_preexisting_server_alone(self):
        port = self._free_port()
        self._listen(port)
        self._point_url(port)
        _, proc = self._install_popen()
        with embeddings.embedding_server(timeout=2) as started:
            self.assertIsNone(started)
        self.assertFalse(proc.terminated)  # nothing was spawned or stopped

    def test_spawn_failure_raises_with_install_hint(self):
        port = self._free_port()
        self._point_url(port)

        def missing_exe(cmd, kwargs):
            raise FileNotFoundError(2, "The system cannot find the file")

        self._install_popen(on_spawn=missing_exe)
        with self.assertRaises(embeddings.EmbedUnavailable) as cm:
            embeddings.ensure_server(timeout=5)
        msg = str(cm.exception)
        self.assertIn("llama-server", msg)
        self.assertIn("winget", msg)

    def test_timeout_stops_process_and_raises(self):
        port = self._free_port()
        self._point_url(port)
        msgs = []
        _, proc = self._install_popen()  # spawns, but never listens
        with self.assertRaises(embeddings.EmbedUnavailable) as cm:
            embeddings.ensure_server(timeout=1.5, progress=msgs.append)
        self.assertIn(f"127.0.0.1:{port}", str(cm.exception))
        self.assertTrue(proc.terminated)  # no orphaned process on timeout
        self.assertTrue(msgs and msgs[0].startswith(
            "waiting for embedding server"))

    def test_stop_server_is_none_safe(self):
        embeddings.stop_server(None)  # must not raise

    def test_stop_server_terminates_and_waits(self):
        p = FakeProc()
        embeddings.stop_server(p)
        self.assertTrue(p.terminated)
        self.assertEqual(p.waits, 1)


if __name__ == "__main__":
    unittest.main()
