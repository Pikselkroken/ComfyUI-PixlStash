"""The Video Saver: ComfyUI's Save Video, saving to PixlStash.

Run over stubs: ``comfy_api`` here is a handful of classes that record what a
schema was built from, and the video is an object that writes a few bytes
where it is told to. So this pins what the node declares and what it asks of
ComfyUI and of PixlStash — which options it offers under which, which file it
encodes where, under what name, with which metadata and quality, and what it
then uploads and files — and nothing about real encoding or about how ComfyUI
turns the nested widgets into the ``format`` dict.
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


def _get_save_image_path(prefix, outdir, width=0, height=0):
    FP.calls.append((prefix, outdir, width, height))
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


class _Spec:
    """One input or output of a schema, as it was declared."""

    def __init__(self, kind, id=None, **kwargs):
        self.kind, self.id, self.kwargs = kind, id, kwargs


def _kind(kind):
    def make(*args, **kwargs):
        return _Spec(kind, *args, **kwargs)

    return types.SimpleNamespace(Input=make, Output=make)


class _NodeOutput:
    def __init__(self, *args, ui=None):
        self.args, self.ui = args, ui


class _WriterWithCrf:
    def save_to(self, path, format=None, codec=None, metadata=None, crf=None):
        pass


class _WriterWithoutCrf:
    def save_to(self, path, format=None, codec=None, metadata=None):
        pass


def _comfy_api():
    io = types.SimpleNamespace(
        ComfyNode=type("ComfyNode", (), {}),
        Schema=lambda **kwargs: types.SimpleNamespace(**kwargs),
        NodeOutput=_NodeOutput,
        Hidden=types.SimpleNamespace(prompt="prompt", extra_pnginfo="extra_pnginfo"),
        Video=_kind("VIDEO"),
        String=_kind("STRING"),
        Boolean=_kind("BOOLEAN"),
        Float=_kind("FLOAT"),
        Custom=_kind,
        DynamicCombo=types.SimpleNamespace(
            Input=lambda id, options, **kwargs: _Spec(
                "DYNAMIC", id, options=options, **kwargs
            ),
            Option=lambda key, inputs: types.SimpleNamespace(key=key, inputs=inputs),
        ),
    )
    latest = types.ModuleType("comfy_api.latest")
    latest.io = io
    latest.Types = types.SimpleNamespace(VideoContainer=_Container, VideoCodec=_Codec)
    latest.Input = types.SimpleNamespace(Video=_WriterWithCrf)
    api = types.ModuleType("comfy_api")
    api.__path__ = []
    api.latest = latest
    return {"comfy_api": api, "comfy_api.latest": latest}


# picture_saver binds numpy/PIL/folder_paths when it is imported, and
# video_saver binds folder_paths and comfy_api; the tests swap video_saver's
# folder_paths for FP.
with boot.patched_modules({**boot.imaging_modules(FP), **_comfy_api()}):
    video_saver = boot.load("nodes.video_saver")
connection = boot.load("connection")

SAVER = video_saver.PixlStashVideoSaver


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def _options(combo):
    """``{option: its nested inputs by id}`` of a nested combo."""
    return {
        opt.key: {spec.id: spec for spec in opt.inputs}
        for opt in combo.kwargs["options"]
    }


class VideoSaverSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = SAVER.define_schema()
        self.inputs = {spec.id: spec for spec in self.schema.inputs}

    def test_identity_and_outputs(self):
        self.assertEqual(self.schema.node_id, "PixlStashVideoSaver")
        self.assertEqual(self.schema.category, "PixlStash")
        self.assertTrue(self.schema.is_output_node)
        self.assertEqual(
            [(o.kind, o.kwargs["display_name"]) for o in self.schema.outputs],
            [("STRING", "picture_ids"), ("VIDEO", "video")],
        )
        self.assertEqual(self.schema.hidden, ["prompt", "extra_pnginfo"])

    def test_inputs_are_save_videos_plus_the_picture_savers(self):
        self.assertEqual(
            [(spec.id, spec.kind) for spec in self.schema.inputs],
            [
                ("video", "VIDEO"),
                ("filename_prefix", "STRING"),
                ("format", "DYNAMIC"),
                ("save_workflow", "BOOLEAN"),
                ("pixlstash_project", "PIXLSTASH_PROJECT"),
                ("pixlstash_set", "PIXLSTASH_SET"),
                ("pixlstash_character", "PIXLSTASH_CHARACTER"),
            ],
        )
        for name in ("pixlstash_project", "pixlstash_set", "pixlstash_character"):
            self.assertTrue(self.inputs[name].kwargs["optional"])
        self.assertTrue(self.inputs["save_workflow"].kwargs["default"])

    def test_every_container_nests_the_codecs_it_can_hold(self):
        formats = _options(self.inputs["format"])
        self.assertEqual(list(formats), ["auto", "mp4", "mkv", "webm"])
        codecs = {name: list(_options(f["codec"])) for name, f in formats.items()}
        self.assertEqual(
            codecs,
            {
                "auto": ["auto", "h264", "av1"],
                "mp4": ["auto", "h264", "av1"],
                "mkv": ["auto", "h264", "av1"],
                "webm": ["auto", "av1"],
            },
        )

    def test_a_codec_nests_an_encoding_mode_with_save_videos_crf(self):
        codecs = _options(_options(self.inputs["format"])["mp4"]["codec"])
        self.assertEqual(codecs["auto"], {})
        crf = {}
        for name in ("h264", "av1"):
            encoding = codecs[name]["encoding"]
            self.assertTrue(encoding.kwargs["optional"])
            modes = _options(encoding)
            self.assertEqual(list(modes), ["auto", "re-encode"])
            self.assertEqual(modes["auto"], {})
            spec = modes["re-encode"]["crf"]
            self.assertEqual(spec.kind, "FLOAT")
            crf[name] = tuple(spec.kwargs[k] for k in ("default", "min", "max"))
        self.assertEqual(crf, {"h264": (18.0, 0.0, 51.0), "av1": (24.0, 0.0, 63.0)})

    def test_no_crf_is_offered_where_the_writer_takes_none(self):
        older = types.SimpleNamespace(Video=_WriterWithoutCrf)
        with mock.patch.object(video_saver, "Input", older):
            inputs = {spec.id: spec for spec in SAVER.define_schema().inputs}
        codecs = _options(_options(inputs["format"])["mp4"]["codec"])
        self.assertEqual(codecs, {"auto": {}, "h264": {}, "av1": {}})

    def test_no_credential_is_declared_as_an_input(self):
        self.assertFalse({"url", "token", "verify_ssl"} & set(self.inputs))

    def test_registered_with_a_server_version(self):
        init = (boot.ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('"PixlStashVideoSaver": PixlStashVideoSaver', init)
        js = (boot.ROOT / "web/js/combo_widgets.js").read_text(encoding="utf-8")
        self.assertRegex(js, r'"PixlStashVideoSaver":\s+"1\.2\.0"')


class _Video:
    """Writes a marker where ComfyUI's video would write the encoded file."""

    def __init__(self, error=None):
        self.saved = []
        self.error = error

    def get_dimensions(self):
        return (96, 64)

    def save_to(self, path, format=None, codec=None, metadata=None, **extra):
        if self.error:
            raise self.error
        self.saved.append(
            types.SimpleNamespace(
                path=path, format=format, codec=codec, metadata=metadata, extra=extra
            )
        )
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


def _format(container="auto", codec="auto", crf=None):
    """The ``format`` value ComfyUI hands the node for these widget choices."""
    chosen = {"codec": codec}
    if crf is not None:
        chosen["encoding"] = {"encoding": "re-encode", "crf": crf}
    return {"format": container, "codec": chosen}


class VideoSaverSaveTests(unittest.TestCase):
    def setUp(self):
        FP.calls.clear()
        self.client = _Client([{"status": "success", "picture_id": 7}])
        self.credentials = ("https://vault.example", "test-token", True)
        for ctx in (
            mock.patch.object(video_saver, "folder_paths", FP),
            mock.patch.object(
                video_saver, "read_credentials", lambda *a: self.credentials
            ),
            mock.patch.object(video_saver, "make_client", lambda *a, **k: self.client),
        ):
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)

    def _save(
        self,
        video,
        fmt=None,
        prefix="clip",
        workflow=True,
        prompt=None,
        info=None,
        **kw,
    ):
        # ComfyUI runs a copy of the class carrying the run's hidden inputs.
        hidden = types.SimpleNamespace(prompt=prompt, extra_pnginfo=info)
        node = type("Run", (SAVER,), {"hidden": hidden})
        return node.execute(video, prefix, fmt or _format(), workflow, **kw)

    def test_encodes_into_the_contained_folder_and_uploads_that_file(self):
        video = _Video()
        hostile = "../../../../tmp/evil"
        out = self._save(video, _format("webm", "av1"), prefix=hostile)

        # Containment is delegated to ComfyUI's helper, with the raw prefix,
        # and the only file written is the one under the folder it returned.
        self.assertEqual(FP.calls, [(hostile, TMP, 96, 64)])
        written = os.path.join(TMP, "contained", "vid_00003.webm")
        self.assertEqual([s.path for s in video.saved], [written])
        self.assertTrue(os.path.isfile(written))
        self.assertEqual(
            (video.saved[0].format, video.saved[0].codec),
            (_Container.WEBM, _Codec.AV1),
        )

        path, is_write, kwargs = self.client.posts[0]
        self.assertEqual(path, "/api/v1/pictures/import")
        self.assertTrue(is_write)
        self.assertEqual(
            kwargs["files"],
            [("file", ("vid_00003.webm", b"encoded-video", "video/webm"))],
        )
        self.assertIsNone(kwargs["data"])

        self.assertEqual(out.args, ("7", video))
        self.assertEqual(
            out.ui,
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
        self.assertTrue(video.saved[0].path.endswith("vid_00003.mp4"))
        self.assertEqual(video.saved[0].format, _Container.MP4)
        self.assertEqual(self.client.posts[0][2]["files"][0][1][2], "video/mp4")

    def test_auto_format_is_a_webm_for_av1_as_in_save_video(self):
        video, explicit = _Video(), _Video()
        self._save(video, _format("auto", "av1"))
        self._save(explicit, _format("mp4", "av1"))
        self.assertTrue(video.saved[0].path.endswith("vid_00003.webm"))
        self.assertEqual(video.saved[0].format, _Container.WEBM)
        # Asked for by name, an MP4 stays an MP4.
        self.assertTrue(explicit.saved[0].path.endswith("vid_00003.mp4"))
        self.assertEqual(explicit.saved[0].format, _Container.MP4)

    def test_crf_reaches_the_writer_only_when_re_encoding(self):
        auto, chosen, bare = _Video(), _Video(), _Video()
        self._save(auto, _format("mp4", "h264"))
        self._save(chosen, _format("mp4", "h264", crf=30.0))
        # An older ComfyUI nests no codec under the format at all.
        self._save(bare, {"format": "mp4"})
        self.assertEqual(auto.saved[0].extra, {})
        self.assertEqual(chosen.saved[0].extra, {"crf": 30.0})
        self.assertEqual(bare.saved[0].extra, {})
        self.assertEqual(bare.saved[0].codec, _Codec.AUTO)

    def test_workflow_metadata_follows_the_toggle(self):
        on, off, bare = _Video(), _Video(), _Video()
        graph = {"workflow": {"nodes": []}}
        self._save(on, prompt={"1": {}}, info=graph)
        self._save(off, workflow=False, prompt={"1": {}}, info=graph)
        self._save(bare)
        self.assertEqual(
            on.saved[0].metadata, {"workflow": {"nodes": []}, "prompt": {"1": {}}}
        )
        self.assertIsNone(off.saved[0].metadata)
        self.assertIsNone(bare.saved[0].metadata)

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

    def test_an_unwired_context_files_nothing(self):
        self._save(_Video(), pixlstash_project=None, pixlstash_set=None)
        self.assertEqual(len(self.client.posts), 1)
        self.assertIsNone(self.client.posts[0][2]["data"])

    def test_a_duplicate_is_filed_but_not_reported_as_new(self):
        self.client.results = [{"status": "duplicate", "picture_id": 9}]
        out = self._save(_Video(), pixlstash_set="5")
        self.assertEqual(out.args[0], "")
        self.assertEqual(self.client.posts[1][2]["json"], {"picture_ids": [9]})

    def test_a_refused_encode_names_the_node_and_uploads_nothing(self):
        video = _Video(error=ValueError("No video stream found in file"))
        with self.assertRaises(RuntimeError) as ctx:
            self._save(video)
        message = str(ctx.exception)
        self.assertIn("PixlStash Video Saver", message)
        self.assertIn("No video stream found in file", message)
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
        self.credentials = ("", "", True)
        with self.assertRaises(RuntimeError) as ctx:
            self._save(video)
        self.assertIn("PixlStash Video Saver", str(ctx.exception))
        self.assertEqual(video.saved, [])

    def test_refuses_under_multi_user(self):
        video = _Video()
        with (
            mock.patch.object(
                video_saver, "read_credentials", connection.read_credentials
            ),
            boot.patched_modules(boot.cli_args_modules(multi_user=True)),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self._save(video)
        self.assertEqual(str(ctx.exception), connection.MULTI_USER_MESSAGE)
        self.assertEqual(video.saved, [])
        self.assertEqual(self.client.posts, [])

    def test_the_picture_savers_uploads_are_still_pngs(self):
        video_saver.PixlStashPictureSaver._upload(self.client, [("a.png", b"png")])
        self.assertEqual(
            self.client.posts[0][2]["files"], [("file", ("a.png", b"png", "image/png"))]
        )


if __name__ == "__main__":
    unittest.main()
