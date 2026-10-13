"""PixlStash Video Saver node.

ComfyUI's Save Video, saving to PixlStash: the same ``video``, ``format``,
``codec`` and encoding inputs, written by the same video writer, with the
file uploaded via the async import endpoint and optionally assigned to a
project, set, and/or character, as the Picture Saver does for images.

Save Video nests the codec under the format and the encoding under the codec,
which takes ComfyUI's V3 node schema, so this node is declared with it where
the rest of the pack uses ``INPUT_TYPES``.

Credentials are read from ComfyUI Settings at run time.  HTTP 403 responses
from any write operation raise a message that explicitly names the
write-scope requirement.
"""

from __future__ import annotations

import inspect
import os

import folder_paths
from comfy_api.latest import Input, Types, io

from ..connection import make_client, read_credentials
from .picture_saver import PixlStashPictureSaver

_NODE = "PixlStash Video Saver"

# Save Video's re-encode quality per codec: (its name, default CRF, highest).
_CRF = {"h264": ("H.264", 18.0, 51.0), "av1": ("AV1", 24.0, 63.0)}


def _codec_input(codecs: list[str]) -> io.DynamicCombo.Input:
    """Save Video's ``codec`` input for one container.

    A codec that can be re-encoded carries an encoding mode, and re-encoding
    carries a CRF — offered only where this ComfyUI's writer takes one.
    """
    takes_crf = "crf" in inspect.signature(Input.Video.save_to).parameters

    def encoding(codec: str) -> list:
        if codec not in _CRF or not takes_crf:
            return []
        name, default, highest = _CRF[codec]
        return [
            io.DynamicCombo.Input(
                "encoding",
                display_name="encoding mode",
                options=[
                    io.DynamicCombo.Option("auto", []),
                    io.DynamicCombo.Option(
                        "re-encode",
                        [
                            io.Float.Input(
                                "crf",
                                default=default,
                                min=0.0,
                                max=highest,
                                step=1.0,
                                tooltip=(
                                    "Lower values produce higher quality "
                                    "and larger files."
                                ),
                            ),
                        ],
                    ),
                ],
                optional=True,
                tooltip=(
                    f"Automatic preserves compatible {name} streams. "
                    "Re-encode applies custom encoding options."
                ),
            )
        ]

    return io.DynamicCombo.Input(
        "codec",
        options=[io.DynamicCombo.Option(c, encoding(c)) for c in codecs],
        tooltip="The output video codec. Auto preserves a compatible source stream.",
    )


class PixlStashVideoSaver(io.ComfyNode):
    """Uploads a video to PixlStash and assigns it to optional contexts.

    Returns the ID of the newly imported video as a string, empty when the
    vault already had it, and passes the video through unchanged, as Save
    Video does.
    """

    @classmethod
    def define_schema(cls):
        # The containers and codecs are the ones the installed ComfyUI can
        # write, paired as Save Video pairs them: WebM takes no H.264.
        codecs = Types.VideoCodec.as_input()
        return io.Schema(
            node_id="PixlStashVideoSaver",
            display_name=_NODE,
            category="PixlStash",
            description="Saves the input video to PixlStash.",
            inputs=[
                io.Video.Input("video", tooltip="The video to save."),
                io.String.Input(
                    "filename_prefix",
                    default="video/comfyui",
                    tooltip="Prefix for the generated video filename.",
                ),
                io.DynamicCombo.Input(
                    "format",
                    options=[
                        io.DynamicCombo.Option(
                            container,
                            [
                                _codec_input(
                                    [
                                        c
                                        for c in codecs
                                        if (container, c) != ("webm", "h264")
                                    ]
                                )
                            ],
                        )
                        for container in Types.VideoContainer.as_input()
                    ],
                    tooltip=(
                        "The output container. Auto uses MP4 for Auto/H.264 "
                        "and WebM for AV1. The others select a specific "
                        "container."
                    ),
                ),
                io.Boolean.Input(
                    "save_workflow",
                    default=True,
                    tooltip=(
                        "Embed the ComfyUI workflow and prompt JSON "
                        "into the video's metadata before uploading."
                    ),
                ),
                io.Custom("PIXLSTASH_PROJECT").Input(
                    "pixlstash_project",
                    optional=True,
                    tooltip="Assign the video to this project at import time.",
                ),
                io.Custom("PIXLSTASH_SET").Input(
                    "pixlstash_set",
                    optional=True,
                    tooltip="Add the video to this set after import.",
                ),
                io.Custom("PIXLSTASH_CHARACTER").Input(
                    "pixlstash_character",
                    optional=True,
                    tooltip="Assign the video to this character after import.",
                ),
            ],
            outputs=[
                io.String.Output(display_name="picture_ids"),
                io.Video.Output(
                    display_name="video", tooltip="The input video, unchanged."
                ),
            ],
            hidden=[io.Hidden.prompt, io.Hidden.extra_pnginfo],
            is_output_node=True,
        )

    @classmethod
    def execute(
        cls,
        video,
        filename_prefix: str,
        format: dict,
        save_workflow: bool,
        pixlstash_project: str = "",
        pixlstash_set: str = "",
        pixlstash_character: str = "",
    ) -> io.NodeOutput:
        # Credentials are resolved server-side from ComfyUI Settings ->
        # PixlStash and never injected into the prompt.
        url, token, verify_ssl = read_credentials()
        if not url or not token:
            raise RuntimeError(
                f"{_NODE}: URL and API Token are required. "
                "Configure them in ComfyUI Settings › PixlStash."
            )
        client = make_client(url, token, verify_ssl)

        # Unpacked as Save Video unpacks them.
        format_name = format["format"]
        codec = format.get("codec") or {"codec": "auto"}
        codec_name = codec["codec"]
        if format_name == "auto":
            format_name = "webm" if codec_name == "av1" else "mp4"
        crf = (codec.get("encoding") or {}).get("crf")

        # Same shape Save Video writes: the workflow keys, then the prompt.
        metadata = None
        if save_workflow:
            metadata = dict(cls.hidden.extra_pnginfo or {})
            if cls.hidden.prompt is not None:
                metadata["prompt"] = cls.hidden.prompt
            metadata = metadata or None

        # Resolve a safe output location.  folder_paths.get_save_image_path
        # refuses a filename_prefix that resolves outside the directory it is
        # given, so a hostile one (e.g. in a shared workflow) cannot write
        # anywhere but under ComfyUI's temp directory.
        width, height = video.get_dimensions()
        full_output_folder, base_name, counter, subfolder, _ = (
            folder_paths.get_save_image_path(
                filename_prefix, folder_paths.get_temp_directory(), width, height
            )
        )
        os.makedirs(full_output_folder, exist_ok=True)

        # Encode into the (contained) temp directory: the file is both the
        # node's preview and what is uploaded.
        extension = Types.VideoContainer.get_extension(format_name)
        fname = f"{base_name}_{counter:05d}.{extension}"
        path = os.path.join(full_output_folder, fname)
        try:
            video.save_to(
                path,
                format=Types.VideoContainer(format_name),
                codec=Types.VideoCodec(codec_name),
                metadata=metadata,
                **({} if crf is None else {"crf": crf}),
            )
        except ValueError as exc:
            # ComfyUI's writer refuses with a bare ValueError, for instance a
            # source it cannot read.
            raise RuntimeError(
                f"{_NODE}: ComfyUI could not write this video as {format_name} "
                f"with the {codec_name} codec — {exc}. If that names the "
                "format or the codec, pick another; otherwise check the video "
                "wired into this node."
            ) from exc

        # ponytail: the whole file is read into memory, as requests would do
        # anyway to build the multipart body. Stream it (requests-toolbelt) if
        # multi-gigabyte videos become normal.
        with open(path, "rb") as fh:
            data = fh.read()

        new_ids = PixlStashPictureSaver._store(
            client,
            [(fname, data)],
            project_id=(pixlstash_project or "").strip(),
            set_id=(pixlstash_set or "").strip(),
            character_id=(pixlstash_character or "").strip(),
            node=_NODE,
        )

        ids_str = ",".join(str(i) for i in new_ids)
        return io.NodeOutput(
            ids_str,
            video,
            ui={
                "images": [{"filename": fname, "subfolder": subfolder, "type": "temp"}],
                "animated": (True,),
                "picture_ids": [ids_str],
            },
        )
