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
                # `_resolve`, not `load_lora`: everything these tests are
                # about (digest validation, credentials, the local-vs-download
                # decision, the 403 message) happens before a MODEL is touched,
                # and going through the apply would need a torch stub to say
                # nothing extra.
                return node._resolve(sha)

    def test_uses_a_usable_local_copy_and_never_downloads(self):
        client = _Client(payload=record())

        def explode(*a, **k):
            raise AssertionError("downloaded a file that was already usable here")

        with mock.patch.object(adapter_loader, "_cached_download", explode):
            shelf_row, path = self._resolve(client)
            triggers = adapter_loader._trigger_words(shelf_row)

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
            _, path = self._resolve(client)
        self.assertEqual(path, "/cache/x.safetensors")

    def test_downloads_when_the_shelf_lists_no_present_copy(self):
        client = _Client(payload=record(locations=[]))
        with mock.patch.object(
            adapter_loader, "_cached_download", lambda c, s: "/cache/x.safetensors"
        ):
            _, path = self._resolve(client)
        self.assertEqual(path, "/cache/x.safetensors")

    def test_trigger_words_survive_a_null(self):
        client = _Client(payload=record(trigger_words=None))
        with mock.patch.object(adapter_loader, "_cached_download", lambda c, s: "x"):
            shelf_row, _ = self._resolve(client)
            triggers = adapter_loader._trigger_words(shelf_row)
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


class LoadLoraTests(unittest.TestCase):
    """What ``load_lora`` does with what ``_resolve`` hands it.

    The resolve half is covered above and the patching half is ComfyUI's; what
    is left — and what actually broke when the node stopped emitting strings —
    is the wiring between them, plus the order the two run in.
    """

    class _Applier:
        def __init__(self):
            self.calls = []

        def apply(self, model, clip, path, strength_model, strength_clip):
            self.calls.append((model, clip, path, strength_model, strength_clip))
            return (f"{model}+lora", f"{clip}+lora" if clip else clip)

    def _node(self, shelf_row=None, path="/loras/x.safetensors"):
        node = adapter_loader.PixlStashAdapterLoader()
        node._applier = self._Applier()
        node._resolve = lambda sha: (shelf_row or record(), path)
        return node

    def test_applies_the_resolved_file_and_passes_both_strengths_through(self):
        node = self._node(path="/loras/knight.safetensors")
        model, clip, _ = node.load_lora(
            "MODEL",
            "— Any —",
            "— None —",
            SHA,
            clip="CLIP",
            strength_model=0.8,
            strength_clip=0.4,
        )
        self.assertEqual(
            node._applier.calls,
            [("MODEL", "CLIP", "/loras/knight.safetensors", 0.8, 0.4)],
        )
        self.assertEqual((model, clip), ("MODEL+lora", "CLIP+lora"))

    def test_a_model_only_graph_carries_no_clip(self):
        # clip stays None all the way through rather than being invented, which
        # is what lets a model-only adapter work with no CLIP wire.
        node = self._node()
        _, clip, _ = node.load_lora("MODEL", "— Any —", "— None —", SHA)
        self.assertIsNone(clip)
        self.assertIsNone(node._applier.calls[0][1])

    def test_the_strengths_default_to_one_like_the_built_in(self):
        node = self._node()
        node.load_lora("MODEL", "— Any —", "— None —", SHA)
        self.assertEqual(node._applier.calls[0][3:], (1.0, 1.0))

    def test_trigger_words_come_out_even_at_zero_strength(self):
        # The built-in returns early on zero strengths and never reads the file.
        # Here the shelf lookup is also what produces trigger_words, so an early
        # return would blank them for anyone who parks a slider at 0.
        node = self._node(shelf_row=record(trigger_words="a knight, plate armour"))
        _, _, triggers = node.load_lora(
            "MODEL", "— Any —", "— None —", SHA, strength_model=0.0, strength_clip=0.0
        )
        self.assertEqual(triggers, "a knight, plate armour")

    def test_a_resolve_failure_is_not_swallowed(self):
        node = adapter_loader.PixlStashAdapterLoader()
        node._applier = self._Applier()

        def boom(sha):
            raise RuntimeError("PixlStash Adapter Loader: no adapter selected.")

        node._resolve = boom
        with self.assertRaises(RuntimeError):
            node.load_lora("MODEL", "— Any —", "— None —", SHA)
        self.assertEqual(node._applier.calls, [], "patched a model anyway")


class TriggerWordTests(unittest.TestCase):
    """The shelf stores a JSON array in a field it declares as a string.

    Every adapter carrying triggers on a real shelf holds `'["Clementine"]'`,
    so passing the value straight through puts brackets and quotes into the
    prompt. Pinned here because the shape comes off the wire and nothing local
    would notice it changing.
    """

    def _words(self, value):
        return adapter_loader._trigger_words({"trigger_words": value})

    def test_a_json_array_is_unwrapped(self):
        self.assertEqual(self._words('["Clementine"]'), "Clementine")
        self.assertEqual(
            self._words('["a knight", "plate armour"]'), "a knight, plate armour"
        )

    def test_a_real_list_still_works(self):
        self.assertEqual(
            self._words(["a knight", "plate armour"]), "a knight, plate armour"
        )

    def test_plain_text_is_left_alone(self):
        self.assertEqual(
            self._words("a knight, plate armour"), "a knight, plate armour"
        )

    def test_a_bare_word_is_not_parsed_as_json(self):
        # `1girl` is a real and very common trigger. json.loads would turn a
        # numeric one into a number, which is why only `[`-leading values are
        # even attempted.
        self.assertEqual(self._words("1girl"), "1girl")

    def test_malformed_json_falls_back_to_the_raw_text(self):
        # Better a visible odd string than an exception at queue time.
        self.assertEqual(self._words('["unclosed'), '["unclosed')

    def test_empty_and_null_are_empty(self):
        for value in (None, "", "   ", [], "[]", '["", "  "]'):
            with self.subTest(value=value):
                self.assertEqual(self._words(value), "")
