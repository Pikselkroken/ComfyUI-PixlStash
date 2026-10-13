"""PixlStash Video Saver node.

The Picture Saver for a VIDEO: encodes it with ComfyUI's own video writer
(the one behind Save Video, so the container, the codec and the embedded
workflow metadata are the same), uploads the file to PixlStash via the async
import endpoint, then optionally assigns it to a project, set, and/or
character.

Credentials are injected via hidden inputs from ComfyUI Settings at
queue time.  HTTP 403 responses from any write operation raise a message
that explicitly names the write-scope requirement.
"""

from __future__ import annotations

import os

import folder_paths

from ..connection import make_client, read_credentials
from .picture_saver import PixlStashPictureSaver

_NODE = "PixlStash Video Saver"


class PixlStashVideoSaver:
    """Uploads a video to PixlStash and assigns it to optional contexts.

    Returns the ID of the newly imported video as a string, empty when the
    vault already had it, and passes the video through unchanged, as Save
    Video does.
    """

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("STRING", "VIDEO")
    RETURN_NAMES = ("picture_ids", "video")
    OUTPUT_NODE = True
    FUNCTION = "save_video"

    @classmethod
    def INPUT_TYPES(cls):
        # The containers and codecs are whatever the installed ComfyUI can
        # write, so the lists never offer one its video writer would refuse.
        from comfy_api.latest import Types

        return {
            "required": {
                "video": ("VIDEO", {"tooltip": "The video to save."}),
                "filename_prefix": (
                    "STRING",
                    {
                        "default": "comfyui",
                        "multiline": False,
                        "tooltip": "Prefix for the generated video filename.",
                    },
                ),
                "format": (
                    Types.VideoContainer.as_input(),
                    {
                        "default": "auto",
                        "tooltip": (
                            "The container to save the video as. Auto is "
                            "WebM for the AV1 codec and MP4 otherwise."
                        ),
                    },
                ),
                "codec": (
                    Types.VideoCodec.as_input(),
                    {
                        "default": "auto",
                        "tooltip": (
                            "The codec to use for the video. Auto keeps a "
                            "stream the container can already hold."
                        ),
                    },
                ),
                "save_workflow": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": (
                            "Embed the ComfyUI workflow and prompt JSON "
                            "into the video's metadata before uploading."
                        ),
                    },
                ),
            },
            "optional": {
                "pixlstash_project": (
                    "PIXLSTASH_PROJECT",
                    {
                        "forceInput": True,
                        "tooltip": "Assign the video to this project at import time.",
                    },
                ),
                "pixlstash_set": (
                    "PIXLSTASH_SET",
                    {
                        "forceInput": True,
                        "tooltip": "Add the video to this set after import.",
                    },
                ),
                "pixlstash_character": (
                    "PIXLSTASH_CHARACTER",
                    {
                        "forceInput": True,
                        "tooltip": "Assign the video to this character after import.",
                    },
                ),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    def save_video(
        self,
        video,
        filename_prefix: str,
        format: str,
        codec: str,
        save_workflow: bool,
        pixlstash_project: str = "",
        pixlstash_set: str = "",
        pixlstash_character: str = "",
        url: str = "",
        token: str = "",
        verify_ssl: bool = True,
        prompt=None,
        extra_pnginfo=None,
    ):
        from comfy_api.latest import Types

        # Credentials are resolved server-side from ComfyUI Settings ->
        # PixlStash and never injected into the prompt.
        url, token, verify_ssl = read_credentials(url, token, verify_ssl)
        if not url or not token:
            raise RuntimeError(
                f"{_NODE}: URL and API Token are required. "
                "Configure them in ComfyUI Settings › PixlStash."
            )
        client = make_client(url, token, verify_ssl)

        # Same shape Save Video writes: the workflow keys, then the prompt.
        metadata = None
        if save_workflow:
            metadata = dict(extra_pnginfo or {})
            if prompt is not None:
                metadata["prompt"] = prompt
            metadata = metadata or None

        # Resolve a safe output location.  folder_paths.get_save_image_path
        # refuses a filename_prefix that resolves outside the directory it is
        # given, so a hostile one (e.g. in a shared workflow) cannot write
        # anywhere but under ComfyUI's temp directory.
        temp_dir = folder_paths.get_temp_directory()
        full_output_folder, base_name, counter, subfolder, _ = (
            folder_paths.get_save_image_path(filename_prefix, temp_dir)
        )
        os.makedirs(full_output_folder, exist_ok=True)
        containers = Types.VideoContainer.as_input()

        # As Save Video: auto is WebM for AV1 where this ComfyUI can write
        # one, and MP4 otherwise.
        if format == "auto" and codec == "av1" and "webm" in containers:
            format = "webm"

        # Encode into the (contained) temp directory: the file is both the
        # node's preview and what is uploaded.
        extension = Types.VideoContainer.get_extension(format)
        fname = f"{base_name}_{counter:05d}.{extension}"
        path = os.path.join(full_output_folder, fname)
        try:
            video.save_to(
                path,
                format=Types.VideoContainer(format),
                codec=Types.VideoCodec(codec),
                metadata=metadata,
            )
        except ValueError as exc:
            # ComfyUI's writer refuses with a bare ValueError: a pair it
            # cannot mux (WebM takes AV1 only), or a source it cannot read.
            raise RuntimeError(
                f"{_NODE}: ComfyUI could not write this video as {format} "
                f"with the {codec} codec — {exc}. If that names the format "
                "or the codec, pick another; otherwise check the video wired "
                "into this node."
            ) from exc

        # ponytail: the whole file is read into memory, as requests would do
        # anyway to build the multipart body. Stream it (requests-toolbelt) if
        # multi-gigabyte videos become normal.
        with open(path, "rb") as fh:
            data = fh.read()

        new_ids = PixlStashPictureSaver._store(
            client,
            [(fname, data)],
            project_id=pixlstash_project.strip(),
            set_id=pixlstash_set.strip(),
            character_id=pixlstash_character.strip(),
            node=_NODE,
        )

        ids_str = ",".join(str(i) for i in new_ids)
        return {
            "ui": {
                "images": [{"filename": fname, "subfolder": subfolder, "type": "temp"}],
                "animated": (True,),
                "picture_ids": [ids_str],
            },
            "result": (ids_str, video),
        }
