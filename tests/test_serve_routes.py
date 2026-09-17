"""The two routes ComfyUI serves *to* PixlStash.

They ship dormant, which is exactly why they are tested: nothing in this
package exercises them, so a mistake here surfaces the day someone else calls
them and not before. Both are an authentication boundary this package invents —
ComfyUI's own server usually has none — and one of them writes a file whose
name came off the wire, so the two things worth pinning are:

* nobody without the configured API token gets an answer from either route, and
* a filename with a separator in it never reaches ``open()``.
"""

import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import types
import unittest
from unittest import mock

import _bootstrap as boot

boot.load_proxy()  # installs the stub aiohttp that serve_routes imports
connection = boot.load("connection")
serve = boot.load("serve_routes")

TOKEN = "test-token-not-a-real-one"

PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_version():
    """The version out of pyproject.toml, found without the code under test.

    ``connection.VERSION`` is a literal, and ``test_server_version`` is what
    keeps it equal to this. Asserting the route against the file rather than
    against the literal means a drift shows up here as well as there, instead
    of the route agreeing with whatever the literal happens to say.
    """
    table = False
    for line in PYPROJECT.read_text(encoding="utf-8").splitlines():
        if line.startswith("["):
            table = line.strip() == "[project]"
        elif table and line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError("pyproject.toml has no [project] version")


def _body(response):
    return json.loads(response.kwargs["body"].decode())


def _status(response):
    return response.kwargs.get("status", 200)


def _configured(token=TOKEN):
    """ComfyUI Settings holding a PixlStash URL and ``token``."""
    return mock.patch.object(
        serve, "read_credentials", lambda: ("https://vault.example", token, True)
    )


class RefusalTests(unittest.TestCase):
    """The shared guard on both routes."""

    def setUp(self):
        patch = mock.patch.object(serve, "multi_user_active", lambda: False)
        patch.start()
        self.addCleanup(patch.stop)

    def _refuse(self, header=None, token=TOKEN):
        headers = {"Authorization": header} if header else {}
        with _configured(token):
            return serve._refusal(boot.FakeRequest(headers=headers))

    def test_the_configured_token_is_let_through(self):
        self.assertIsNone(self._refuse(f"Bearer {TOKEN}"))

    def test_no_header_is_refused(self):
        self.assertEqual(_status(self._refuse()), 401)

    def test_another_scheme_is_refused(self):
        self.assertEqual(_status(self._refuse(f"Token {TOKEN}")), 401)

    def test_a_different_token_is_refused(self):
        # The whole point of the guard: ComfyUI's HTTP server is routinely open
        # on a LAN, so "anyone who can reach the port" must not be enough.
        self.assertEqual(_status(self._refuse("Bearer test-wrong-token")), 401)

    def test_a_prefix_of_the_token_is_refused(self):
        self.assertEqual(_status(self._refuse(f"Bearer {TOKEN[:-1]}")), 401)

    def test_an_unconfigured_comfyui_refuses_everyone_rather_than_nobody(self):
        # Empty settings must not degrade into "any token matches the empty
        # one". 503, because no request could have succeeded.
        self.assertEqual(_status(self._refuse(f"Bearer {TOKEN}", token="")), 503)

    def test_multi_user_is_refused_before_the_token_is_read(self):
        with mock.patch.object(serve, "multi_user_active", lambda: True):
            with mock.patch.object(
                serve,
                "read_credentials",
                mock.Mock(side_effect=AssertionError("read credentials anyway")),
            ):
                response = serve._refusal(
                    boot.FakeRequest(headers={"Authorization": f"Bearer {TOKEN}"})
                )
        self.assertEqual(_status(response), 400)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(serve, "multi_user_active", lambda: False)
        patch.start()
        self.addCleanup(patch.stop)

    def _inventory(self, folders):
        fp = types.ModuleType("folder_paths")

        def get_filename_list(kind):
            if kind not in folders:
                raise KeyError(kind)
            return list(folders[kind])

        fp.get_filename_list = get_filename_list
        with mock.patch.dict("sys.modules", {"folder_paths": fp}), _configured():
            response = asyncio.run(
                serve.inventory(
                    boot.FakeRequest(headers={"Authorization": f"Bearer {TOKEN}"})
                )
            )
        return _body(response)

    def test_reports_the_package_version_and_every_shelf_kind(self):
        body = self._inventory({"checkpoints": ["b.safetensors", "a.safetensors"]})
        # Against pyproject.toml and not against serve.VERSION, which would
        # be comparing the code to itself.
        self.assertEqual(body["package_version"], _pyproject_version())
        self.assertRegex(body["package_version"], r"^\d+\.\d+")
        # The other half of the handshake the client does outbound: the floor a
        # caller needs before it submits a prompt these nodes would refuse.
        self.assertEqual(body["min_server_version"], connection.MIN_SERVER_VERSION)
        self.assertRegex(body["min_server_version"], r"^\d+\.\d+")
        self.assertEqual(
            sorted(body["models"]), ["checkpoints", "loras", "text_encoders", "vae"]
        )
        # Sorted, so two ComfyUI installs with the same files compare equal.
        self.assertEqual(
            body["models"]["checkpoints"], ["a.safetensors", "b.safetensors"]
        )

    def test_a_folder_this_comfyui_does_not_register_is_empty_not_an_error(self):
        self.assertEqual(self._inventory({})["models"]["loras"], [])

    def test_an_old_comfyui_s_clip_folder_is_reported_as_text_encoders(self):
        # The folder was renamed; the node loaders already know both names, and
        # a caller should not have to.
        body = self._inventory({"clip": ["t5.safetensors"]})
        self.assertEqual(body["models"]["text_encoders"], ["t5.safetensors"])

    def test_it_is_behind_the_same_guard(self):
        with _configured():
            response = asyncio.run(serve.inventory(boot.FakeRequest(headers={})))
        self.assertEqual(_status(response), 401)


class _Field:
    """One multipart field, handing its body over in two chunks."""

    def __init__(self, name, filename, data):
        self.name = name
        self.filename = filename
        self._chunks = [data[: len(data) // 2], data[len(data) // 2 :], b""]

    async def read_chunk(self, size=None):
        return self._chunks.pop(0)


class _Reader:
    def __init__(self, fields):
        self._fields = list(fields)

    async def next(self):
        return self._fields.pop(0) if self._fields else None


class _MultipartRequest(boot.FakeRequest):
    def __init__(self, fields, headers=None):
        super().__init__(headers=headers)
        self._fields = fields

    async def multipart(self):
        return _Reader(self._fields)


class AssetUploadTests(unittest.TestCase):
    def setUp(self):
        self.input_dir = tempfile.mkdtemp(prefix="pixlstash_assets_test_")
        self.addCleanup(shutil.rmtree, self.input_dir, True)

        fp = types.ModuleType("folder_paths")
        fp.get_input_directory = lambda: self.input_dir
        patches = [
            mock.patch.object(serve, "multi_user_active", lambda: False),
            mock.patch.dict("sys.modules", {"folder_paths": fp}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _upload(self, filename, data=b"PNGDATA", field_name="file", header=None):
        headers = {"Authorization": header or f"Bearer {TOKEN}"}
        request = _MultipartRequest(
            [_Field(field_name, filename, data)], headers=headers
        )
        with _configured():
            return asyncio.run(serve.upload_asset(request))

    def _under_input(self, *parts):
        return os.path.join(self.input_dir, *parts)

    def test_a_file_lands_in_the_asset_subfolder_and_is_named_back(self):
        body = _body(self._upload("reference.png"))
        self.assertEqual(
            body,
            {
                "name": "reference.png",
                "subfolder": serve.ASSET_SUBFOLDER,
                "type": "input",
            },
        )
        written = self._under_input(serve.ASSET_SUBFOLDER, "reference.png")
        self.assertTrue(os.path.isfile(written))
        with open(written, "rb") as fh:
            self.assertEqual(fh.read(), b"PNGDATA")
        # The .part it was streamed into is gone, not left beside it.
        self.assertFalse(os.path.isfile(written + ".part"))

    def test_a_hostile_name_is_refused_by_the_rule_and_by_the_route(self):
        # Both halves matter. Asserting only that a directory the traversal was
        # never going to reach is still empty passes just as happily when the
        # traversal landed somewhere else, so the rule is asserted directly and
        # the route is asserted to be using it.
        for name in (
            "../../../../etc/cron.d/pwn",
            "..",
            "sub/dir.png",
            "sub\\dir.png",
            "C:\\Users\\me\\evil.png",
            ".hidden",
            "",
            "a" * 129,
            "naughty\nname.png",
            "trailing.",
            "trailing ",
            # Windows opens these as devices; the write would succeed against
            # one and the rename would then fail with an unhandled OSError.
            "NUL.png",
            "con",
            "COM1.png",
        ):
            with self.subTest(filename=name):
                self.assertIsNone(serve._asset_name(name))
                self.assertEqual(_status(self._upload(name)), 400)

        # And the route wrote nothing anywhere beneath the input directory.
        self.assertEqual(list(os.walk(self.input_dir))[0][2], [])

    def test_an_ordinary_name_with_a_space_is_not_hostile(self):
        # The containment rule refusing "my photo.png" would be a bug of its
        # own: accepting the caller's reference image is the route's whole job.
        self.assertEqual(serve._asset_name("my photo.png"), "my photo.png")
        self.assertEqual(_status(self._upload("my photo.png")), 200)

    def test_a_body_without_a_file_field_is_a_400(self):
        self.assertEqual(_status(self._upload("ok.png", field_name="notfile")), 400)

    def test_an_oversized_body_is_refused_and_leaves_nothing_behind(self):
        with mock.patch.object(serve, "MAX_ASSET_BYTES", 4):
            response = self._upload("big.png", data=b"0123456789")
        self.assertEqual(_status(response), 413)
        self.assertEqual(os.listdir(self._under_input(serve.ASSET_SUBFOLDER)), [])

    def test_a_malformed_body_is_a_400_and_not_a_413(self):
        # aiohttp raises ValueError for a truncated or badly bounded multipart
        # body. Reporting that as "larger than the 64 MB limit" sends whoever
        # reads it looking for a file that is not the problem.
        class _Broken(_Field):
            async def read_chunk(self, size=None):
                raise ValueError("Invalid boundary")

        request = _MultipartRequest(
            [_Broken("file", "ok.png", b"x")],
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with _configured():
            response = asyncio.run(serve.upload_asset(request))
        self.assertEqual(_status(response), 400)
        self.assertEqual(os.listdir(self._under_input(serve.ASSET_SUBFOLDER)), [])

    def test_an_existing_asset_is_never_replaced(self):
        # A prompt already queued against this name must not start loading
        # different bytes than it was submitted against.
        self.assertEqual(_status(self._upload("shot.png", data=b"first")), 200)
        self.assertEqual(_status(self._upload("shot.png", data=b"second")), 409)
        with open(self._under_input(serve.ASSET_SUBFOLDER, "shot.png"), "rb") as fh:
            self.assertEqual(fh.read(), b"first")

    def test_a_symlink_left_at_the_part_path_is_not_written_through(self):
        # O_CREAT|O_EXCL|O_NOFOLLOW: anything that can pre-create the .part —
        # another process, a shared or NFS input directory — must not be able
        # to redirect this write out of the input directory.
        target = os.path.join(self.input_dir, "elsewhere.txt")
        with open(target, "wb") as fh:
            fh.write(b"untouched")
        os.makedirs(self._under_input(serve.ASSET_SUBFOLDER), exist_ok=True)
        os.symlink(target, self._under_input(serve.ASSET_SUBFOLDER, "bait.png.part"))

        self.assertEqual(_status(self._upload("bait.png")), 409)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"untouched")

    def test_it_is_behind_the_same_guard(self):
        response = self._upload("ok.png", header="Bearer test-wrong-token")
        self.assertEqual(_status(response), 401)
        self.assertFalse(os.path.isdir(self._under_input(serve.ASSET_SUBFOLDER)))


if __name__ == "__main__":
    unittest.main()
