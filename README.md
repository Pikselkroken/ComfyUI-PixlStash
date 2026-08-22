<div align="center">
  <a href="https://pixlstash.dev"><img src="Logo.png" alt="PixlStash" width="120" /></a>
  <h1>ComfyUI-PixlStash</h1>
  <p>Custom ComfyUI nodes for loading and saving images to a PixlStash vault.</p>
  <p>
    <a href="https://pixlstash.dev"><strong>pixlstash.dev</strong></a>
    &nbsp;&nbsp;|&nbsp;&nbsp;
    <a href="https://github.com/Pikselkroken/pixlstash"><strong>github.com/Pikselkroken/pixlstash</strong></a>
  </p>
</div>

---

<div align="center">
  <a href="examples/PixlStash-SearchImageEdit.json"><img src="screenshots/ScreenshotSearchEdit.jpg" alt="Semantic search feeding a ComfyUI image-edit pipeline" width="100%"></a>
  <p><sub>Semantic search → image edit, end to end. <a href="examples/PixlStash-SearchImageEdit.json">Open this workflow</a>.</sub></p>
</div>

![Example workflow](screenshots/Workflow.png)

[Download example workflow](PixlStash-LoadAndSave.json)

## Overview

ComfyUI-PixlStash connects your ComfyUI workflows directly to a PixlStash vault. You can browse and load images by project, set, or character, run them through any pipeline, and save the results back with full metadata and optional workflow embedding.

Connection credentials (URL and API token) are configured once in **ComfyUI Settings > PixlStash** and are read by the nodes at runtime. They never appear as node widgets or in saved workflow JSON.

## Nodes

### Project Loader

Selects a project from your vault. Outputs a `PIXLSTASH_PROJECT` wire that can be passed to other nodes to scope their operations.

### Set Loader

Selects a set within a project. Outputs `PIXLSTASH_PROJECT` and `PIXLSTASH_SET` wires. Reference-character sets are excluded from the dropdown.

### Character Loader

Selects a character from your vault. Outputs `PIXLSTASH_PROJECT` and `PIXLSTASH_CHARACTER` wires.
Note this requires PixlStash v1.2.1+ to function properly as it relies on an API addition in 1.2.1.

### Picture Loader

Loads images from PixlStash as `IMAGE` and `MASK` tensors.

Two modes of operation:

- **Picker mode** -- click the Browse button to open a thumbnail browser, select one or more images, and the node loads exactly those.
- **Browse mode** -- leave the selection empty and the node fetches images automatically based on any connected project, set, or character filters.

Outputs the loaded images together with pass-through `PIXLSTASH_PROJECT`, `PIXLSTASH_SET`, and `PIXLSTASH_CHARACTER` wires so you can forward context to a downstream saver without extra wiring.

### Picture Saver

Uploads images to PixlStash and optionally assigns them to a project, set, and/or character. Supports embedded workflow metadata in PNG output. Returns the IDs of successfully imported pictures as a comma-separated string.

### Likeness Search

Search for likeness to a provided face with facial features comparison. Add the face image with LoadImage or use the PixlStash Picture Loader to load it from the PixlStash database. The following uses a picture not in the PixlStash database.

![Face Likeness](screenshots/FaceLikenessSearch.jpg)

You can also use image embedding search with multiple images so you can combine concepts. An old man and a young man drinking beer. The result here is 4 older men drinking beer.

![Picture Likeness](screenshots/MultiLikenessSearch.jpg)

You can filter by project, character and set by providing those inputs.

**Note:** Requires PixlStash v1.4 (for now only available as development releases)

### Face Likeness Gate

Keeps only the generations that actually match a reference character. Wire a batch of generated images plus a **Character Loader** into the gate, set a likeness `threshold`, and it splits the batch into two streams — `accepted` (faces at or above the threshold) and `rejected` (off‑model renders or frames with no detectable face) — along with `accepted_count` / `rejected_count`. Route `accepted` into an upscale / save branch and send `rejected` to a "scrapheap" preview, so you never waste compute polishing a bad match.

Face likeness is scored server‑side by a single stateless endpoint: the node uploads the frames in batches, the server detects and embeds each face in‑memory on the GPU and scores it against the character's reference faces, and returns one score per frame. Nothing is imported or persisted, so scoring is fast and leaves the vault untouched — there's nothing to clean up. The reference character is passed through so an accepted branch can be saved back tagged to the same character without re‑wiring.

**Note:** Requires PixlStash v1.6.0+ (which added the stateless `score_character_likeness` endpoint the gate uses) and a running face‑extraction worker. A read-scope token is sufficient.

### Picture Likeness Gate

Splits a batch of generations into `accepted` and `rejected` outputs by judging each frame's whole-image likeness against a reference **picture set**. Wire a batch of generated images plus a **Set Loader** into the gate, pick a `combine` mode and a `threshold`, and route `accepted` into an upscale / save branch while `rejected` goes nowhere or to a "rejects" saver — so you never waste compute polishing an off-target render. Also outputs `accepted_count` / `rejected_count`, and passes the reference set through so an accepted branch can be saved straight back into the same set.

Each frame is scored against every member of the set. The `combine` mode decides how those per-member scores become one verdict — the default `min` means **must match all** (a `[monkey, banana, bicycle]` reference set keeps only frames that resemble all three), while `max` matches any one and the means fall in between.

Scoring is read-only and synchronous: each frame is sent as the query image to PixlStash's image-likeness search with the set as the corpus, so the server embeds it on the fly and ranks it against the set's members. **Nothing is uploaded to your vault, nothing is persisted, and no write scope is needed** — there's no import step and no embedding-readiness wait. (Cost is one request per frame; the reference set must have ≤ 500 members.)

**Note:** Requires PixlStash v1.4 (for now only available as development releases). A read-scope token is sufficient.

### Semantic Search

Search using a text string and the node will use PixlStash's semantic search feature to extract pictures based on similarity to the search.

![Semantic Search](screenshots/SemanticSearch.jpg)

You can filter by project, character and set by providing those inputs.

**Note:** Requires PixlStash v1.4 (for now only available as development releases)

### Adapter (LoRA) Loader

Applies a LoRA (or LoKr, LoHa, OFT, DoRA) from the PixlStash **model shelf** to your model — picked by browsing a grid, not by hunting for a filename in a dropdown of hundreds.

It has the same shape as ComfyUI's built-in LoRA loader: `model` and `clip` in, `model` and `clip` out, `strength_model` and `strength_clip`, and you chain them for several adapters. The only difference is where the file comes from — the `lora_name` dropdown is replaced by **Browse adapters…**, and the file is resolved off the shelf (or fetched from it) instead of read out of a local folder. A third output, `trigger_words`, carries whatever the shelf recorded for the adapter; wire it into a text encode.

Click **Browse adapters…** to open a thumbnail grid of the adapters on your shelf, showing each one's picture, name and base model. The picture is the adapter's own icon if it has one; almost none do, so for an adapter attached to a character or a set it falls back to that character's or set's thumbnail — a LoRA of a person is easier to spot by their face than by two letters. An adapter that is attached to nothing and carries no icon draws a generated mark instead. A LoRA that was trained in several epochs shows up as one card, not one per file: the grid draws the stack's cover — the file the shelf itself would load — and says how many files the run holds. Narrow the grid with the `adapter_kind` and `base_model` dropdowns, with the in-modal search box, or by wiring a Set Loader or Character Loader so you see only the adapters attached to that character or set. If both a set and a character are wired, the grid follows the character — the server accepts one or the other, never both.

Once you have picked one, the node wears it: the button reads the adapter's name, and the picture from the grid is drawn on the node itself, so a graph holding four loaders is readable at a glance instead of being four identical buttons. A saved workflow stores only the digest, so the name and the picture are looked up from the shelf when the node loads — an unreachable server or a missing token leaves the start of the digest on the button and no picture, and nothing else breaks. The checkpoint, VAE and text-encoder loaders do the same.

`adapter_kind` and `base_model` filter the Browse grid only. They do not affect what is loaded, so changing one does not disturb a selection you already made.

`clip` is optional so model-only adapters work without a CLIP wire. ComfyUI can't vary a node's outputs per graph, so the CLIP *output* still exists in that case and carries nothing — leave it unconnected when you leave the input unconnected.

**Where the file comes from.** The shelf records the paths of the machine *PixlStash* runs on. A copy is used in place only if all of this holds on the machine ComfyUI is running on: the shelf last saw that copy as `present`, the path stays inside the folder it was registered under, it ends in `.safetensors`, it is on disk here, and its size is exactly the size the shelf recorded. Anything else — including a shelf record that carries no size at all — is treated as unverifiable and fetched instead. That is deliberately strict: the alternative is loading whatever unrelated file happens to sit at the same path on a ComfyUI host that isn't the PixlStash host, which is silently wrong output rather than an error.

Fetched adapters go into `<your first loras directory>/pixlstash/<sha256>.safetensors`, are verified against the SHA-256 before anything is written under that name, and are re-used from there afterwards. They also show up in ComfyUI's stock LoRA dropdown as 64-character hex names, and nothing evicts them.

**Note:** Requires PixlStash v1.10 — the release that introduces both the model shelf and the route that serves adapter bytes. On an older server the node still works when ComfyUI and PixlStash share a filesystem (the file is used in place), and reports that the file could not be fetched otherwise. Fetching is `local_owner_only` on the server: an owner token from loopback, your LAN or Tailscale, which covers a normal split-host setup but not a ComfyUI reaching a PixlStash across the open internet. The Browse grid marks entries PixlStash itself has no copy of, but it cannot tell whether *your* ComfyUI can see a copy the server can — so on a split-host setup an unmarked entry can still fail at queue time.

**Token scope:** the shelf routes are `OWNER_ONLY` and pinned to a library, which is stricter than the routes the other nodes use. A resource-scoped share token that works fine with the Picture Loader will get a 403 here — use an owner token. The node says so in as many words when you queue it; the Browse modal shows the server's generic "no access" message.

### Checkpoint Loader

Loads a checkpoint from the model shelf, browsing a grid with the names, base models and icons the shelf records instead of a dropdown of filenames. Same three outputs as the built-in Load Checkpoint — `MODEL`, `CLIP`, `VAE`.

**This one cannot fetch the file.** PixlStash serves adapter bytes but not checkpoint bytes, so the copy has to be readable on the machine ComfyUI runs on — the ordinary case when the two share a filesystem, and never the case when they do not. It is also addressed by the shelf's `id` rather than by hash, because a 24 GB checkpoint is listable long before the server has finished hashing it.

A bare diffusion model — a Flux UNET, say — is filed as a checkpoint by PixlStash's parameter-count rule, so it turns up in the grid too. It loads here as a `MODEL` with the `CLIP` and `VAE` outputs empty; wire those from their own loaders.

### VAE Loader

Loads a VAE from the model shelf. Drop-in for the built-in Load VAE, with **Browse VAEs…** in place of the dropdown. Resolved off the shelf, or fetched once and cached under `<your first vae directory>/pixlstash/<sha256>.safetensors`, verified against the digest before anything is written.

TAESD and the other `vae_approx` previews are not here — PixlStash deliberately excludes them from the `vae` kind, so they never appear on the shelf. Use ComfyUI's own Load VAE for those.

### CLIP Loader

Loads a text encoder from the model shelf — one file for SD and SDXL, or two for the models that need a pair (Flux, SD3 and HiDream take clip-l beside a T5 or a Llama). ComfyUI splits that into `CLIPLoader` and `DualCLIPLoader` because each takes its filenames off a dropdown; here both come off the Browse grid, so the second slot is simply optional.

`type` is the model family the encoder is being loaded for. The list is read from ComfyUI's own `CLIPType` at load time rather than copied out of it, so it never lags a release. A workflow saved against a newer ComfyUI still opens: an unknown value falls back to `stable_diffusion` instead of failing validation.

Files are cached under `<your first text_encoders directory>/pixlstash/` on the same terms as the others.

## Workflow examples

Ready-to-load workflow JSON files live in the [`examples/`](examples/) directory. Click any screenshot to open its workflow.

### Search → Image Edit

Pull source images out of the vault by meaning, then run them through an edit pipeline.

[![Semantic search feeding a ComfyUI image-edit pipeline](screenshots/ScreenshotImageEdit.jpg)](examples/PixlStash-SearchImageEdit.json)

→ [PixlStash-SearchImageEdit.json](examples/PixlStash-SearchImageEdit.json)

### Outpaint

Extend a loaded vault image beyond its original frame.

[![Outpainting a vault image in ComfyUI](screenshots/ScreenshotOutpaint.jpg)](examples/PixlStash-Outpaint.json)

→ [PixlStash-Outpaint.json](examples/PixlStash-Outpaint.json)

### Face Likeness Gate

Filter a batch of generations down to only the frames that match a reference character, previewing the accepted and rejected streams side by side.

[![Face Likeness Gate splitting a batch into accepted and rejected by character match](screenshots/FaceLikenessGate.jpg)](examples/PixlStash-FaceLikenessGate.json)

→ [PixlStash-FaceLikenessGate.json](examples/PixlStash-FaceLikenessGate.json)

Or run it end to end: generate with a character LoRA, gate by face likeness, then upscale the accepted frames and save the accepted and rejected streams back to separate sets.

[![Generate with a character LoRA, gate by face likeness, then upscale and save the matches](screenshots/FaceLikenessGateUpscale.jpg)](examples/PixlStash-FaceLikenessGate-Upscale.json)

→ [PixlStash-FaceLikenessGate-Upscale.json](examples/PixlStash-FaceLikenessGate-Upscale.json)

### Picture Likeness Gate

Generate and keep only the pictures that match a reference picture set, and preview the accepted and rejected streams side by side. No vault writes required.

→ [PixlStash-PictureLikenessGate.json](examples/PixlStash-PictureLikenessGate.json)

Or run it end to end: generate a batch, gate it against a reference picture set, then upscale the accepted frames and save the accepted and rejected streams back to separate sets.

[![Generate a batch, gate against a reference picture set, then upscale and save the matches](screenshots/ScreenshotPictureLikenessGate.jpg)](examples/PixlStash-PictureLikenessGateUpscale.json)

→ [PixlStash-PictureLikenessGateUpscale.json](examples/PixlStash-PictureLikenessGateUpscale.json)

### Upscale

Upscale a vault image and save the result back with metadata intact.

[![Upscaling a vault image in ComfyUI](screenshots/ScreenshotUpscale.jpg)](examples/PixlStash-Upscale.json)

→ [PixlStash-Upscale.json](examples/PixlStash-Upscale.json)

### Generate from the model shelf

Every model in this graph comes off the PixlStash shelf: the checkpoint, the VAE and the text encoder are each picked from a Browse grid rather than a dropdown, and the render goes straight back into a project.

[![Flux.2 Klein 9B generating from shelf-loaded models in ComfyUI](screenshots/PixlStash-Flux2-Models.jpg)](examples/PixlStash-Flux2-Models.json)

→ [PixlStash-Flux2-Models.json](examples/PixlStash-Flux2-Models.json)

The sampler half lives in a `Text to Image (Flux.2 Klein 9B)` subgraph, so the graph reads as what it is about: three shelf loaders in, one image out, saved to a project. It needs a ComfyUI new enough to have subgraphs — this one was saved on frontend 1.47.11 (ComfyUI 0.30).

A good illustration of why the three loaders are separate nodes. Flux.2 Klein 9B is a **bare diffusion model** — the shelf files it as a checkpoint on its parameter count, and ComfyUI cannot build a CLIP or a VAE out of it — so only the Checkpoint Loader's `model` output is wired, and `clip` and `vae` come from their own shelf loaders beside it. The CLIP Loader is set to `flux2` with one encoder; its second slot stays empty.

## Installation

### Via ComfyUI Manager (recommended)

Search for **ComfyUI-PixlStash** in the Custom Nodes Manager and click Install.

![Install via ComfyUI Manager](screenshots/ScreenshotInstallation.jpg)

### Manual

Clone this repository into your ComfyUI `custom_nodes` directory:

```bash
cd custom_nodes
git clone https://github.com/Pikselkroken/ComfyUI-PixlStash.git
```

After installation, restart ComfyUI and configure your PixlStash URL and API token under **Settings > PixlStash**.

## Configuration

| Setting | Description |
|---|---|
| URL | Base URL of your PixlStash instance |
| API Token | Token with the required read or write scope |
| Verify SSL | Whether to validate the server certificate |

> **Multi-user is not supported.** PixlStash doesn't work with ComfyUI's
> `--multi-user` mode. ComfyUI doesn't tell a node which user submitted the
> running prompt, so the nodes can't pick that user's token and refuse to run
> rather than risk using someone else's. Run a separate single-user ComfyUI
> instance for each PixlStash user.

## Development

Run the test suite (stdlib `unittest`, no running ComfyUI required):

```bash
python -m unittest discover -s tests
```

The tests stub ComfyUI's runtime modules, so only `requests` needs to be
installed (`pip install -r requirements.txt`). They cover the security-sensitive
paths: the multi-user guard, the proxy SSRF/auth checks (including the shelf
routes' digest guard and query forwarding), loader id extraction, Picture Saver
path containment, and the Adapter (LoRA) Loader's path containment and download
digest verification.

Lint and format with [ruff](https://docs.astral.sh/ruff/):

```bash
ruff check .
ruff format .
```

## License

Open Source MIT License. See [LICENSE](LICENSE).

### Third-party code

**No ComfyUI source is copied into this repository.** ComfyUI is GPL-3.0, this package is MIT, and the two stay apart because everything here is written against ComfyUI's *public* APIs — the same ones every custom-node pack calls:

| What this package uses | Where |
|---|---|
| `comfy.utils.load_torch_file`, `comfy.sd.VAE`, `comfy.sd.load_clip`, `comfy.sd.CLIPType`, `comfy.sd.load_lora_for_models`, `comfy.sd.load_checkpoint_guess_config`, `comfy.sd.load_diffusion_model` | the model-shelf loaders |
| `folder_paths` (models directories, user directory) | loaders, credential lookup |
| `server.PromptServer.instance.routes` | the `/pixlstash/*` proxy |
| `app.registerExtension`, the LiteGraph node API | `web/js/*` |

The loader nodes deliberately **mirror the shape** of ComfyUI's built-ins — `LoraLoader`, `CheckpointLoaderSimple`, `VAELoader`, `CLIPLoader`/`DualCLIPLoader` — in their inputs, outputs and widget names, so they drop into a graph where a built-in already sits. That is an interface, not an implementation: none of their code is reproduced here. The same goes for the `IMAGE`/`MASK` tensor conventions (`[N,H,W,3]` float32 in `[0,1]`), which are ComfyUI's documented data format that any node must produce to interoperate, and for identifiers such as `CLIPType` member names, which have to match to call the API at all.

Runtime dependencies are installed from PyPI, never vendored here: [requests](https://pypi.org/project/requests/) (Apache-2.0) and [Pillow](https://pypi.org/project/Pillow/) (MIT-CMU). ComfyUI itself supplies `torch` and `numpy`.
