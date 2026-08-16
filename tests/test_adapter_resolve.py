"""``PixlStashAdapterLoader.resolve`` — what it does after the sha256 guard.

The guards themselves live in test_adapter_path.py and test_adapter_download.py;
this pins the wiring between them: that a usable local copy is preferred to a
download, that the fallback happens when it isn't usable, that both declared
outputs carry what they claim, and that the two error paths a user is most
likely to hit say something useful.
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import _bootstrap as boot

adapter_loader = boot.load("nodes.adapter_loader")

TMP = tempfile.mkdtemp(prefix="pixlstash_resolve_test_")
FOLDER = os.path.join(TMP, "loras")
LOCAL = os.path.join(FOLDER, "local.safetensors")
PAYLOAD = b"local-adapter-bytes"
SHA = "b" * 64


def setUpModule():
    os.makedirs(FOLDER, exist_ok=True)
    with open(LOCAL, "wb") as fh:
        fh.write(PAYLOAD)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def record(**overrides):
    base = {
        "sha256": SHA,
        "file_size": len(PAYLOAD),
        "trigger_words": "a knight, plate armour",
        "locations": [
            {"folder_path": FOLDER, "relpath": "local.safetensors", "state": "present"}
        ],
    }
    base.update(overrides)
    return base


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.paths = []

    def get(self, path, **kwargs):
        self.paths.append(path)
        if self.error is not None:
            raise self.error
        return _Response(self.payload)


class ResolveTests(unittest.TestCase):
    def _resolve(self, client, sha=SHA, url="https://vault.example", token="t"):
        node = adapter_loader.PixlStashAdapterLoader()
        with mock.patch.object(
            adapter_loader, "read_credentials", lambda: (url, token, True)
        ):
            with mock.patch.object(adapter_loader, "make_client", lambda *a: client):
                return node.resolve("— Any —", "— None —", sha)

    def test_uses_a_usable_local_copy_and_never_downloads(self):
        client = _Client(payload=record())

        def explode(*a, **k):
            raise AssertionError("downloaded a file that was already usable here")

        with mock.patch.object(adapter_loader, "_cached_download", explode):
            path, triggers, _ = self._resolve(client)

        self.assertEqual(path, os.path.normpath(LOCAL))
        self.assertEqual(triggers, "a knight, plate armour")
        # The record is re-read by hash, never trusted from the workflow.
        self.assertEqual(client.paths, [f"/api/v1/adapters/{SHA}"])

    def test_downloads_when_no_local_copy_is_usable(self):
        # Size mismatch: the path exists here but holds something else.
        client = _Client(payload=record(file_size=999_999))
        with mock.patch.object(
            adapter_loader, "_cached_download", lambda c, s: "/cache/x.safetensors"
        ):
            path, _, _ = self._resolve(client)
        self.assertEqual(path, "/cache/x.safetensors")

    def test_downloads_when_the_shelf_lists_no_present_copy(self):
        client = _Client(payload=record(locations=[]))
        with mock.patch.object(
            adapter_loader, "_cached_download", lambda c, s: "/cache/x.safetensors"
        ):
            path, _, _ = self._resolve(client)
        self.assertEqual(path, "/cache/x.safetensors")

    def test_trigger_words_survive_a_null(self):
        client = _Client(payload=record(trigger_words=None))
        with mock.patch.object(adapter_loader, "_cached_download", lambda c, s: "x"):
            _, triggers, _ = self._resolve(client)
        self.assertEqual(triggers, "")

    def test_uppercase_and_padded_digests_are_normalised(self):
        client = _Client(payload=record())
        with mock.patch.object(adapter_loader, "_cached_download", lambda c, s: "x"):
            self._resolve(client, sha=f"  {SHA.upper()}  ")
        self.assertEqual(client.paths, [f"/api/v1/adapters/{SHA}"])

    def test_a_403_names_the_owner_token_requirement(self):
        client = _Client(
            error=RuntimeError(
                "PixlStash: token does not have access to this resource."
            )
        )
        with self.assertRaises(RuntimeError) as ctx:
            self._resolve(client)
        message = str(ctx.exception)
        self.assertIn("owner token", message)
        self.assertIn("owner-only", message)

    def test_other_http_errors_are_not_disguised_as_a_scope_problem(self):
        client = _Client(error=RuntimeError("PixlStash: invalid or expired API token."))
        with self.assertRaises(RuntimeError) as ctx:
            self._resolve(client)
        self.assertIn("invalid or expired", str(ctx.exception))
        self.assertNotIn("owner token", str(ctx.exception))

    def test_a_non_record_response_is_a_pixlstash_error_not_an_attributeerror(self):
        for payload in ([], None, "nope"):
            with self.subTest(payload=payload):
                with self.assertRaises(RuntimeError) as ctx:
                    self._resolve(_Client(payload=payload))
                self.assertIn("did not return an adapter record", str(ctx.exception))

    def test_missing_credentials_are_refused_before_any_request(self):
        client = _Client(payload=record())
        with self.assertRaises(RuntimeError) as ctx:
            self._resolve(client, token="")
        self.assertIn("API Token are required", str(ctx.exception))
        self.assertEqual(client.paths, [])


if __name__ == "__main__":
    unittest.main()
