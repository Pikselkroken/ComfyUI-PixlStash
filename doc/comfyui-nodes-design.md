# Design Document: ComfyUI-PixlStash Nodes

## Overview

Two custom ComfyUI nodes that connect a ComfyUI workflow directly to a PixlStash vault.

| Node | Purpose | Required token scope |
|---|---|---|
| **PixlStash Image Loader** | Replaces `LoadImage` — fetches one or more images from PixlStash | `READ` (or wider) |
| **PixlStash Image Saver** | Replaces `SaveImage` — uploads images to PixlStash and attaches workflow metadata | `ALL` (write access) |

Both nodes must support HTTPS with self-signed certificates (user-configurable SSL verification bypass) and authenticate via Bearer token.

---

## Shared Configuration

Every node exposes the following connection settings, either as node inputs or as a global ComfyUI extension settings panel (preferred for reuse):

| Setting | Type | Notes |
|---|---|---|
| `pixlstash_url` | `STRING` | Base URL, e.g. `https://192.168.1.10:8000` |
| `api_token` | `STRING` | Bearer token value — stored in ComfyUI settings, not embedded in saved workflows |
| `verify_ssl` | `BOOLEAN` | Default `true`. Set `false` to accept self-signed/untrusted certificates. |

### Authentication

All requests must include:

```
Authorization: Bearer <api_token>
```

No other authentication mechanism should be used. Cookie-based sessions are not appropriate for a headless node environment.

> **Scope note:** The server enforces scope automatically based on the token. A `READ`-scoped token silently restricts listing and image fetches to the token's target resource (a set, project, or character). A full `ALL`-scoped token has unrestricted access. Nodes should not need to inspect scope themselves — they just pass the token and work with whatever the server returns.

---

## Node 1: PixlStash Image Loader

### ComfyUI node definition

```
CLASS_TYPES   = "PixlStashImageLoader"
DISPLAY_NAME  = "PixlStash Image Loader"
CATEGORY      = "image/input"
RETURN_TYPES  = ("IMAGE", "MASK", "STRING")
RETURN_NAMES  = ("image", "mask", "picture_id")
FUNCTION      = "load_images"
```

When more than one image is selected, the node outputs a batched `IMAGE` tensor (shape `[N, H, W, C]`). The `picture_id` output is a comma-separated string of the selected integer IDs, in selection order.

### Inputs

| Input | Widget type | Notes |
|---|---|---|
| `picture_ids` | Hidden string (managed by the picker UI) | Comma-separated integer IDs |
| `set_id` | `INT` optional | Pre-filter the picker to a specific picture set |
| `sort` | `COMBO` | `score_desc`, `imported_desc`, `random` — matches PixlStash sort keys |
| `limit` | `INT` | Max pictures to load from the selection (default 1) |

### Picker UI (custom widget)

The node should register a custom widget that opens a modal browser inside ComfyUI. The browser must:

1. Load a paginated thumbnail grid from PixlStash.
2. Support multi-select (click to toggle selection, shift-click for range).
3. Show the currently selected count and allow clearing.
4. On confirm, write the selected IDs into the hidden `picture_ids` input.

Thumbnail previews for selected pictures should also be shown inline on the node itself (same pattern as ComfyUI's built-in `LoadImage` preview).

---

### API endpoints used by the Loader

#### 1. List picture sets (populate set filter dropdown)

```
GET /picture_sets
Authorization: Bearer <token>
```

**Response (array):**
```jsonc
[
  {
    "id": 3,
    "name": "Training Set A",
    "picture_count": 142,
    "thumbnail_url": "/picture_sets/3/thumbnail"
  }
]
```

A scoped token will automatically restrict this to the token's accessible set(s). The node should use the result to populate the optional set filter combo.

---

#### 2. List / browse pictures (populate the picker grid)

```
GET /pictures
Authorization: Bearer <token>

Query parameters:
  fields=grid          (compact projection: id, format, score, …)
  sort=<sort_key>      (e.g. imported_at, score — see /sort_mechanisms)
  descending=true
  offset=<int>
  limit=<int>          (page size, suggest 48)
  set_id=<int>         (optional, filter to one set)
```

**Response (array of picture objects with grid fields):**
```jsonc
[
  {
    "id": 101,
    "format": "png",
    "score": 4,
    "width": 1024,
    "height": 1024,
    "imported_at": "2026-03-12T10:22:00"
  }
]
```

Pagination: repeat with increasing `offset` until the returned array is shorter than `limit`.

---

#### 3. Get picture thumbnail (picker grid thumbnails and node preview)

```
GET /pictures/thumbnails/{id}.webp
Authorization: Bearer <token>
```

Returns a WebP image. Use this for all preview rendering inside the picker and on the node itself. Do not fetch the full-resolution image just for previews.

---

#### 4. Get picture metadata (optional — for richer picker display)

```
GET /pictures/{id}/metadata
Authorization: Bearer <token>
```

Returns the full picture record including `tags`, `description`, `score`, embedded metadata etc. Only needed if the picker shows a detail panel for a selected image.

---

#### 5. Download full-resolution image (actual node output)

```
GET /pictures/{id}.{ext}
Authorization: Bearer <token>
```

where `ext` is the value of the `format` field returned in step 2 (e.g. `png`, `jpg`, `webp`).

Returns the raw image bytes. Decode with PIL and convert to a ComfyUI `IMAGE` tensor (float32, range 0–1, shape `[1, H, W, C]`).

If `format` is unknown or unavailable, the node should fall back to PIL's `Image.open()` with the raw byte stream.

---

#### 6. Get available sort mechanisms (populate sort combo)

```
GET /sort_mechanisms
Authorization: Bearer <token>
```

Returns a list of sort key descriptors. Use to dynamically populate the `sort` combo input rather than hard-coding keys.

---

## Node 2: PixlStash Image Saver

### ComfyUI node definition

```
CLASS_TYPES   = "PixlStashImageSaver"
DISPLAY_NAME  = "PixlStash Image Saver"
CATEGORY      = "image/output"
RETURN_TYPES  = ("STRING",)
RETURN_NAMES  = ("picture_ids",)
OUTPUT_NODE   = True
FUNCTION      = "save_images"
```

The node returns the IDs of the newly created pictures as a comma-separated string, so they can be piped into downstream nodes (e.g. a tag writer or set assigner).

### Inputs

| Input | Type | Notes |
|---|---|---|
| `images` | `IMAGE` | Batched tensor from upstream |
| `set_id` | `INT` optional | Add saved pictures to this set immediately |
| `score` | `INT` optional | Pre-assign score (0–5); omit to leave unscored |
| `filename_prefix` | `STRING` | Prefix for generated filenames (default `"comfyui"`) |
| `save_workflow` | `BOOLEAN` | Embed the serialised ComfyUI workflow into the file (default `true`) |

### Workflow and metadata embedding

When `save_workflow` is enabled, the node must:

1. Retrieve the current workflow JSON from the ComfyUI API (`GET /object_info` or the prompt context available in the `server` object).
2. Encode the workflow JSON as a PNG `tEXt` chunk with key `workflow` before uploading. This is the same convention used by ComfyUI's built-in `SaveImage` node and ensures the workflow survives a round-trip through PixlStash's storage.
3. Include a second `tEXt` chunk with key `prompt` containing the serialised prompt/node graph, matching ComfyUI's own export format.

If the output format is JPEG or WebP (no lossless metadata chunks), embed the workflow JSON as EXIF `UserComment` (tag `0x9286`).

---

### API endpoints used by the Saver

#### 1. Upload image(s)

```
POST /pictures/import
Authorization: Bearer <token>
Content-Type: multipart/form-data

Fields:
  file        (one or more UploadFile parts)
  project_id  (optional INT form field)
```

This is an async endpoint. It returns a `task_id` immediately:

```jsonc
{ "task_id": "b3f1e2a0-..." }
```

---

#### 2. Poll import status until complete

```
GET /pictures/import/status?task_id=<task_id>
Authorization: Bearer <token>
```

**Response:**
```jsonc
{
  "status": "in_progress" | "completed" | "failed",
  "stage": "hashing" | "importing" | "done",
  "total": 4,
  "processed": 2,
  "progress": 50.0,
  "results": [               // only present when status == "completed"
    { "id": 201, "file_name": "comfyui_00001.png" }
  ]
}
```

Poll at a reasonable interval (e.g. 500 ms). On `"failed"`, raise a node error with the `error` field from the response.

---

#### 3. Add saved pictures to a set (if `set_id` is provided)

```
POST /picture_sets/{set_id}/members
Authorization: Bearer <token>
Content-Type: application/json

{
  "picture_ids": [201, 202, 203]
}
```

Only call this after the import task completes and the `results` array is available.

---

#### 4. Set score on saved pictures (if `score` is provided)

```
PATCH /pictures/{id}
Authorization: Bearer <token>
Content-Type: application/json

{ "score": 4 }
```

One PATCH per picture. Issue these after import completes. The server will reject the request if the token scope does not allow write access to the picture.

---

## Error handling

| Condition | Behaviour |
|---|---|
| HTTP 401 | Raise a clear node error: "PixlStash: invalid or expired API token." |
| HTTP 403 | Raise: "PixlStash: token does not have access to this resource." |
| HTTP 404 | Raise: "PixlStash: picture/set not found." |
| SSL error with `verify_ssl=true` | Raise: "PixlStash: SSL certificate verification failed. Set verify_ssl=false if using a self-signed certificate." |
| Import task status `"failed"` | Raise with the server-supplied `error` message. |
| Timeout (suggest 30 s per request) | Raise with a timeout message, include the URL attempted. |

Never silently swallow HTTP errors or fall back to a default value. Surface the status code and response body in the error message to aid debugging.

---

## Security notes

- **Never log or store the raw token value.** Use it only in the `Authorization` header.
- The token should be stored in ComfyUI's settings store (encrypted at rest where the host supports it), not hard-coded in node defaults or saved into exported workflow JSON.
- `verify_ssl=false` is intentionally supported for self-hosted setups with self-signed certificates, but the UI should display a visible warning when it is disabled.
- All HTTP requests should set a `User-Agent` of `ComfyUI-PixlStash/<version>` to aid server-side debugging.

---

## Repository layout

Both nodes live in a single repository. ComfyUI's custom node loader scans `ComfyUI/custom_nodes/` for subdirectories containing an `__init__.py` and imports `NODE_CLASS_MAPPINGS` from it — the number of nodes registered is unlimited.

```
ComfyUI-PixlStash/
  __init__.py          # registers both nodes; exposes NODE_CLASS_MAPPINGS and WEB_DIRECTORY
  loader.py            # PixlStashImageLoader
  saver.py             # PixlStashImageSaver
  connection.py        # shared HTTP client, auth, SSL handling, error raising
  web/
    js/
      picker.js        # custom picker widget (loaded automatically via WEB_DIRECTORY)
  requirements.txt     # requests or httpx, Pillow — both already present in most ComfyUI envs
  README.md
```

`__init__.py` must export the two standard dicts and declare the web directory:

```python
from .loader import PixlStashImageLoader
from .saver import PixlStashImageSaver

NODE_CLASS_MAPPINGS = {
    "PixlStashImageLoader": PixlStashImageLoader,
    "PixlStashImageSaver":  PixlStashImageSaver,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PixlStashImageLoader": "PixlStash Image Loader",
    "PixlStashImageSaver":  "PixlStash Image Saver",
}

WEB_DIRECTORY = "web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
```

`WEB_DIRECTORY = "web"` causes ComfyUI to serve every file under `web/` automatically, so `picker.js` is loaded by the frontend without any separate install step.

### Installation methods

| Method | Steps |
|---|---|
| **ComfyUI Manager** | Add the repo URL to Manager's custom node list; users install with one click and get auto-updates |
| **Manual / git** | `git clone <repo> ComfyUI/custom_nodes/ComfyUI-PixlStash` then restart ComfyUI |
| **pip (optional)** | Add a `pyproject.toml` with a `comfyui_nodes` entry point; `pip install` into the ComfyUI venv |

---

## Dependency notes

The nodes require only packages already present in a standard ComfyUI environment:

- `requests` (or `httpx` for async) — HTTP client
- `Pillow` — image encode/decode and PNG metadata chunk writing
- `torch`, `numpy` — tensor conversion (already present in ComfyUI)

No additional dependencies should be introduced.
