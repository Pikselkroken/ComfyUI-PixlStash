"""The download cache is content-addressed, so it must never cache the wrong bytes.

``_cached_download`` names the file ``<sha256>.safetensors`` and treats the
file's *existence* as the whole validity check on every later run. That is only
safe because nothing is put under that name until its digest has been verified.
These tests pin exactly that: a truncated body, a substituted file, or a
connection that dies mid-stream must leave the cache empty — not a permanently
trusted wrong file and not an orphaned ``.part``.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import types
import unittest

import _bootstrap as boot

adapter_loader = boot.load("nodes.adapter_loader")

TMP = tempfile.mkdtemp(prefix="pixlstash_download_test_")
LORAS = os.path.join(TMP, "loras")
CACHE = os.path.join(LORAS, "pixlstash")

PAYLOAD = b"safetensors-bytes" * 100
SHA = hashlib.sha256(PAYLOAD).hexdigest()


_saved_folder_paths = None


def setUpModule():
    global _saved_folder_paths
    os.makedirs(LORAS, exist_ok=True)
    fp = types.ModuleType("folder_paths")
    fp.get_folder_paths = lambda kind: [LORAS] if kind == "loras" else []
    # Other test modules install their own folder_paths stub; put whatever was
    # there back, so this file can't depend on (or disturb) discovery order.
    _saved_folder_paths = sys.modules.get("folder_paths")
    sys.modules["folder_paths"] = fp


def tearDownModule():
    if _saved_folder_paths is None:
        sys.modules.pop("folder_paths", None)
    else:
        sys.modules["folder_paths"] = _saved_folder_paths
    shutil.rmtree(TMP, ignore_errors=True)


class _Stream:
    """Stands in for a streamed requests.Response.

    Watches the cache directory while the body is still arriving. That is the
    only way to pin the write-under-.part property: a SIGKILL mid-stream runs
    no ``except`` block, so the tests that assert on cleanup after an exception
    cannot tell a temp name from the final one.
    """

    def __init__(self, chunks, boom=None, watch=None):
        self._chunks = chunks
        self._boom = boom
        self._watch = watch
        self.closed = False
        self.seen_midstream = []

    def iter_content(self, chunk_size=None):
        for chunk in self._chunks:
            if self._watch is not None and os.path.isdir(self._watch):
                self.seen_midstream.append(sorted(os.listdir(self._watch)))
            yield chunk
        if self._boom:
            raise self._boom

    def close(self):
        self.closed = True


class _Client:
    def __init__(self, stream):
        self.stream = stream

    def get(self, path, **kwargs):
        assert kwargs.get("stream") is True, "the body must be streamed, not buffered"
        return self.stream


def final_path(sha=SHA):
    return os.path.join(CACHE, f"{sha}.safetensors")


def leftovers():
    return sorted(os.listdir(CACHE)) if os.path.isdir(CACHE) else []


class DownloadTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(CACHE, ignore_errors=True)

    def test_caches_a_body_whose_digest_matches(self):
        stream = _Stream([PAYLOAD[:50], PAYLOAD[50:]])
        path = adapter_loader._cached_download(_Client(stream), SHA)

        self.assertEqual(path, final_path())
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), PAYLOAD)
        self.assertEqual(leftovers(), [f"{SHA}.safetensors"], "a .part was left behind")
        self.assertTrue(stream.closed, "the streamed response was never closed")

    def test_the_content_addressed_name_never_exists_before_the_digest_matches(self):
        """The property the whole module docstring rests on.

        A power cut or SIGKILL mid-download runs no cleanup at all, so the only
        thing that keeps a half-file from becoming a permanently trusted cache
        entry is that the bytes were never under that name in the first place.
        """
        stream = _Stream([PAYLOAD[:50], PAYLOAD[50:]], watch=CACHE)
        adapter_loader._cached_download(_Client(stream), SHA)

        self.assertTrue(stream.seen_midstream, "never observed the write in progress")
        for listing in stream.seen_midstream:
            self.assertNotIn(
                f"{SHA}.safetensors",
                listing,
                "unverified bytes were written under the content-addressed name",
            )
            self.assertIn(f"{SHA}.safetensors.part", listing)

    def test_refuses_and_caches_nothing_when_the_body_is_truncated(self):
        # A 200 with a short body is the case that would otherwise poison the
        # cache permanently: the name asserts a digest the content doesn't have.
        with self.assertRaises(RuntimeError) as ctx:
            adapter_loader._cached_download(_Client(_Stream([PAYLOAD[:50]])), SHA)

        self.assertIn("SHA-256", str(ctx.exception))
        self.assertFalse(os.path.exists(final_path()))
        self.assertEqual(leftovers(), [])

    def test_refuses_an_empty_body(self):
        with self.assertRaises(RuntimeError):
            adapter_loader._cached_download(_Client(_Stream([])), SHA)
        self.assertFalse(os.path.exists(final_path()))
        self.assertEqual(leftovers(), [])

    def test_refuses_a_substituted_file(self):
        other = b"a completely different adapter"
        with self.assertRaises(RuntimeError) as ctx:
            adapter_loader._cached_download(_Client(_Stream([other])), SHA)
        self.assertIn("not the", str(ctx.exception))
        self.assertEqual(leftovers(), [])

    def test_a_failure_after_bytes_are_written_still_removes_the_part_file(self):
        """The cleanup path with a partial file actually on disk.

        The tests that raise from ``client.get`` never create a ``.part`` at
        all, so their "nothing left behind" assertion holds whether or not the
        cleanup runs. This one raises mid-stream, so the file exists when the
        handler fires and the assertion has something to catch.
        """
        stream = _Stream([PAYLOAD[:50]], boom=RuntimeError("PixlStash: 500"))
        with self.assertRaises(RuntimeError):
            adapter_loader._cached_download(_Client(stream), SHA)
        self.assertTrue(os.path.isdir(CACHE), "never got as far as writing")
        self.assertEqual(leftovers(), [], "left a half-written .part behind")

    def test_a_dropped_connection_leaves_no_part_file(self):
        # requests raises ConnectionError (an OSError), never a RuntimeError,
        # from inside iter_content — a bare `except RuntimeError` would miss it
        # and orphan the partial file.
        boom = ConnectionError("connection reset")
        with self.assertRaises(ConnectionError):
            adapter_loader._cached_download(
                _Client(_Stream([PAYLOAD[:50]], boom=boom)), SHA
            )
        self.assertEqual(leftovers(), [])

    def test_a_404_names_the_version_that_added_the_route(self):
        class _Failing:
            def get(self, path, **kwargs):
                raise RuntimeError("PixlStash: not found — /api/v1/adapters/x/file")

        with self.assertRaises(RuntimeError) as ctx:
            adapter_loader._cached_download(_Failing(), SHA)
        message = str(ctx.exception)
        self.assertIn("no usable copy", message)
        self.assertIn("1.10", message)
        self.assertEqual(leftovers(), [])

    def test_no_readable_copy_is_reported_in_the_server_s_own_words(self):
        # 409, not 404: the route exists and the shelf knows the hash, it just
        # can't reach a copy. Blaming the server's version here would be wrong,
        # so the hint must stay off and the server's detail must come through.
        detail = (
            "PixlStash: HTTP 409 from https://vault.example/api/v1/adapters/x/file. "
            'Response: {"detail":"This adapter is on the shelf but no copy of it '
            'is readable on this machine right now."}'
        )

        class _Failing:
            def get(self, path, **kwargs):
                raise RuntimeError(detail)

        with self.assertRaises(RuntimeError) as ctx:
            adapter_loader._cached_download(_Failing(), SHA)
        message = str(ctx.exception)
        self.assertIn("no copy of it is readable", message)
        self.assertNotIn("1.10", message)
        self.assertEqual(leftovers(), [])

    def test_other_failures_are_not_blamed_on_the_server_s_version(self):
        # A bad token, an SSL failure or a timeout says nothing about whether
        # the server serves that route; claiming otherwise sends the user
        # looking in the wrong place.
        for detail in (
            "PixlStash: invalid or expired API token.",
            "PixlStash: SSL certificate verification failed.",
            "PixlStash: request timed out for https://vault.example.",
        ):
            with self.subTest(detail=detail):

                class _Failing:
                    def get(self, path, **kwargs):
                        raise RuntimeError(detail)

                with self.assertRaises(RuntimeError) as ctx:
                    adapter_loader._cached_download(_Failing(), SHA)
                message = str(ctx.exception)
                self.assertIn(detail, message)
                self.assertNotIn("1.10", message)
        self.assertEqual(leftovers(), [])

    def test_a_cache_hit_makes_no_request(self):
        class _Exploding:
            def get(self, path, **kwargs):
                raise AssertionError("re-downloaded a file already in the cache")

        os.makedirs(CACHE, exist_ok=True)
        with open(final_path(), "wb") as fh:
            fh.write(PAYLOAD)

        self.assertEqual(
            adapter_loader._cached_download(_Exploding(), SHA), final_path()
        )


class CacheDirTests(unittest.TestCase):
    def test_refuses_when_comfyui_has_no_loras_directory(self):
        fp = sys.modules["folder_paths"]
        original = fp.get_folder_paths
        fp.get_folder_paths = lambda kind: []
        try:
            with self.assertRaises(RuntimeError) as ctx:
                adapter_loader._cache_dir()
            self.assertIn("no 'loras' model directory", str(ctx.exception))
        finally:
            fp.get_folder_paths = original


if __name__ == "__main__":
    unittest.main()
