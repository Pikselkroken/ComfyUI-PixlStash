"""The Video Saver: the Picture Saver's contract, for a VIDEO.

Run over stubs: ``comfy_api`` here is three enum-shaped classes and the video
is an object that writes a few bytes where it is told to. So this pins what the
node asks of ComfyUI and of PixlStash — which file it encodes where, under what
name, with which metadata, and what it then uploads and files — and nothing
about real encoding.
"""

import enum
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

import _bootstrap as boot

TMP = tempfile.mkdtemp(prefix="pixlstash_video_test_")

FP = types.ModuleType("folder_paths")
FP.calls = []
FP.get_temp_directory = lambda: TMP


def _get_save_image_path(prefix, outdir):
    FP.calls.append((prefix, outdir))
    return (os.path.join(outdir, "contained"), "vid", 3, "sub", "")


FP.get_save_image_path = _get_save_image_path


class _Container(str, enum.Enum):
    AUTO = "auto"
    MP4 = "mp4"
    MKV = "mkv"
    WEBM = "webm"

    @classmethod
    def as_input(cls):
        return [m.value for m in cls]

    @classmethod
    def get_extension(cls, value):
        return "mp4" if cls(value) is cls.AUTO else cls(value).value


class _Codec(str, enum.Enum):
    AUTO = "auto"
    H264 = "h264"
    AV1 = "av1"

    @classmethod
    def as_input(cls):
        return [m.value for m in cls]


def _comfy_api():
    latest = types.ModuleType("comfy_api.latest")
    latest.Types = types.SimpleNamespace(VideoContainer=_Container, VideoCodec=_Codec)
    api = types.ModuleType("comfy_api")
    api.__path__ = []
    api.latest = latest
    return {"comfy_api": api, "comfy_api.latest": latest}


# picture_saver binds numpy/PIL/folder_paths when it is imported, and
# video_saver binds folder_paths; the tests swap video_saver's for FP.
with boot.patched_modules(boot.imaging_modules(FP)):
    video_saver = boot.load("nodes.video_saver")
connection = boot.load("connection")

SAVER = video_saver.PixlStashVideoSaver


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class _Video:
    """Writes a marker where ComfyUI's video would write the encoded file."""

    def __init__(self, error=None):
        self.saved = []
        self.error = error

    def save_to(self, path, format=None, codec=None, metadata=None):
        if self.error:
            raise self.error
        self.saved.append((path, format, codec, metadata))
        with open(path, "wb") as fh:
            fh.write(b"encoded-video")


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    """PixlStash as far as the savers use it: one import, done at once."""

    def __init__(self, results):
        self.results = results
        self.posts = []

    def post(self, path, *, is_write=False, **kwargs):
        self.posts.append((path, is_write, kwargs))
        return _Response({"task_id": "task-1"})

    def get(self, path, **kwargs):
        return _Response({"status": "completed", "results": self.results})


class VideoSaverContractTests(unittest.TestCase):
    def setUp(self):
        ctx = boot.patched_modules(_comfy_api())
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.spec = SAVER.INPUT_TYPES()

    def test_outputs_and_function_agree(self):
        self.assertEqual(SAVER.RETURN_TYPES, ("STRING", "VIDEO"))
        self.assertEqual(SAVER.RETURN_NAMES, ("picture_ids", "video"))
        self.assertTrue(SAVER.OUTPUT_NODE)
        self.assertTrue(callable(getattr(SAVER, SAVER.FUNCTION)))

    def test_inputs_are_save_videos_plus_the_picture_savers(self):
        self.assertEqual(
            list(self.spec["required"]),
            ["video", "filename_prefix", "format", "codec", "save_workflow"],
        )
        self.assertEqual(self.spec["required"]["video"][0], "VIDEO")
        self.assertEqual(
            list(self.spec["optional"]),
            ["pixlstash_project", "pixlstash_set", "pixlstash_character"],
        )

    def test_format_and_codec_are_read_from_comfyuis_enums(self):
        self.assertEqual(
            self.spec["required"]["format"][0], ["auto", "mp4", "mkv", "webm"]
        )
        self.assertEqual(self.spec["required"]["codec"][0], ["auto", "h264", "av1"])

    def test_no_credential_is_declared_as_an_input(self):
        names = [n for group in self.spec.values() for n in group]
        self.assertFalse({"url", "token", "verify_ssl"} & set(names))

    def test_registered_with_a_server_version(self):
        init = (boot.ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('"PixlStashVideoSaver": PixlStashVideoSaver', init)
        js = (boot.ROOT / "web/js/combo_widgets.js").read_text(encoding="utf-8")
        self.assertRegex(js, r'"PixlStashVideoSaver":\s+"1\.2\.0"')


class VideoSaverSaveTests(unittest.TestCase):
    def setUp(self):
        FP.calls.clear()
        self.client = _Client([{"status": "success", "picture_id": 7}])
        for ctx in (
            boot.patched_modules(_comfy_api()),
            boot.patched_modules(boot.cli_args_modules(multi_user=False)),
            mock.patch.object(video_saver, "folder_paths", FP),
            mock.patch.object(video_saver, "make_client", lambda *a, **k: self.client),
        ):
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)

    def _save(
        self, video, prefix="clip", fmt="auto", codec="auto", workflow=True, **kw
    ):
        kw.setdefault("url", "https://vault.example")
        kw.setdefault("token", "test-token")
        return SAVER().save_video(video, prefix, fmt, codec, workflow, **kw)

    def test_encodes_into_the_contained_folder_and_uploads_that_file(self):
        video = _Video()
        hostile = "../../../../tmp/evil"
        out = self._save(video, prefix=hostile, fmt="webm", codec="av1")

        # Sanitisation is delegated to ComfyUI's helper, with the raw prefix,
        # and the only file written is the one under the folder it returned.
        self.assertEqual(FP.calls, [(hostile, TMP)])
        written = os.path.join(TMP, "contained", "vid_00003.webm")
        self.assertEqual([s[0] for s in video.saved], [written])
        self.assertTrue(os.path.isfile(written))
        self.assertEqual(video.saved[0][1:3], (_Container.WEBM, _Codec.AV1))

        path, is_write, kwargs = self.client.posts[0]
        self.assertEqual(path, "/api/v1/pictures/import")
        self.assertTrue(is_write)
        self.assertEqual(
            kwargs["files"],
            [("file", ("vid_00003.webm", b"encoded-video", "video/webm"))],
        )
        self.assertIsNone(kwargs["data"])

        self.assertEqual(out["result"], ("7", video))
        self.assertEqual(
            out["ui"],
            {
                "images": [
                    {"filename": "vid_00003.webm", "subfolder": "sub", "type": "temp"}
                ],
                "animated": (True,),
                "picture_ids": ["7"],
            },
        )

    def test_auto_format_is_an_mp4(self):
        video = _Video()
        self._save(video)
        self.assertTrue(video.saved[0][0].endswith("vid_00003.mp4"))
        self.assertEqual(video.saved[0][1], _Container.AUTO)
        self.assertEqual(self.client.posts[0][2]["files"][0][1][2], "video/mp4")

    def test_auto_format_is_a_webm_for_av1_as_in_save_video(self):
        video, explicit = _Video(), _Video()
        self._save(video, codec="av1")
        self._save(explicit, fmt="mp4", codec="av1")
        self.assertTrue(video.saved[0][0].endswith("vid_00003.webm"))
        self.assertEqual(video.saved[0][1:3], (_Container.WEBM, _Codec.AV1))
        # Asked for by name, an MP4 stays an MP4.
        self.assertTrue(explicit.saved[0][0].endswith("vid_00003.mp4"))
        self.assertEqual(explicit.saved[0][1], _Container.MP4)

    def test_the_picture_savers_uploads_are_still_pngs(self):
        video_saver.PixlStashPictureSaver._upload(self.client, [("a.png", b"png")])
        self.assertEqual(
            self.client.posts[0][2]["files"], [("file", ("a.png", b"png", "image/png"))]
        )

    def test_workflow_metadata_follows_the_toggle(self):
        on, off, bare = _Video(), _Video(), _Video()
        graph = {"workflow": {"nodes": []}}
        self._save(on, prompt={"1": {}}, extra_pnginfo=graph)
        self._save(off, workflow=False, prompt={"1": {}}, extra_pnginfo=graph)
        self._save(bare)
        self.assertEqual(
            on.saved[0][3], {"workflow": {"nodes": []}, "prompt": {"1": {}}}
        )
        self.assertIsNone(off.saved[0][3])
        self.assertIsNone(bare.saved[0][3])

    def test_files_the_video_under_project_set_and_character(self):
        self._save(
            _Video(),
            pixlstash_project=" 4 ",
            pixlstash_set="5",
            pixlstash_character="6",
        )
        self.assertEqual(self.client.posts[0][2]["data"], {"project_id": "4"})
        self.assertEqual(
            [(p, k.get("json")) for p, _, k in self.client.posts[1:]],
            [
                ("/api/v1/picture_sets/5/members", {"picture_ids": [7]}),
                ("/api/v1/characters/6/faces", {"picture_ids": [7]}),
            ],
        )

    def test_a_duplicate_is_filed_but_not_reported_as_new(self):
        self.client.results = [{"status": "duplicate", "picture_id": 9}]
        out = self._save(_Video(), pixlstash_set="5")
        self.assertEqual(out["result"][0], "")
        self.assertEqual(self.client.posts[1][2]["json"], {"picture_ids": [9]})

    def test_an_unwritable_pair_names_the_node_and_uploads_nothing(self):
        video = _Video(error=ValueError("WebM output requires the AV1 codec"))
        with self.assertRaises(RuntimeError) as ctx:
            self._save(video, fmt="webm", codec="h264")
        message = str(ctx.exception)
        self.assertIn("PixlStash Video Saver", message)
        self.assertIn("WebM output requires the AV1 codec", message)
        self.assertEqual(self.client.posts, [])

    def test_a_missing_project_names_this_node(self):
        def refuse(path, **kwargs):
            raise RuntimeError("PixlStash: HTTP 404 — Project 4 not found")

        self.client.post = refuse
        with self.assertRaises(RuntimeError) as ctx:
            self._save(_Video(), pixlstash_project="4")
        self.assertIn("PixlStash Video Saver: project 4", str(ctx.exception))

    def test_refuses_without_credentials_before_encoding(self):
        video = _Video()
        with mock.patch.object(
            video_saver, "read_credentials", lambda *a: ("", "", True)
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._save(video)
        self.assertIn("PixlStash Video Saver", str(ctx.exception))
        self.assertEqual(video.saved, [])

    def test_refuses_under_multi_user(self):
        video = _Video()
        with boot.patched_modules(boot.cli_args_modules(multi_user=True)):
            with self.assertRaises(RuntimeError) as ctx:
                self._save(video)
        self.assertEqual(str(ctx.exception), connection.MULTI_USER_MESSAGE)
        self.assertEqual(video.saved, [])
        self.assertEqual(self.client.posts, [])


if __name__ == "__main__":
    unittest.main()
