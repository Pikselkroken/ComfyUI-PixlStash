# PixlStash example workflows

Drop these `.json` files onto the ComfyUI canvas (or use **Workflow → Open**). They use only PixlStash nodes plus core ComfyUI nodes (`LoadImage`, `PreviewImage`, samplers), so they load on any install once the PixlStash extension is present. The one exception is `PixlStash-Flux2-Models.json`, which wraps its sampler half in a **subgraph** and so needs a ComfyUI new enough to have them — it was saved on frontend 1.47.11 (ComfyUI 0.30).

Before running, set your vault **URL** and **API Token** under **Settings → PixlStash**. The dropdowns on the loader nodes populate live from your vault, so pick your own project / set / character after loading — the saved selections are placeholders.

| File | Showcases | Flow |
|---|---|---|
| `PixlStash-SemanticSearch.json` | Semantic Search | Project Loader → Semantic Search (text query) → Preview |
| `PixlStash-LikenessSearch.json` | Likeness Search (face mode) | Load Image (query face) + Project Loader → Likeness Search → Preview |
| `PixlStash-CurateRoundtrip.json` | Loaders + Picture Loader + Picture Saver | Project → Set / Character → Picture Loader → Preview **and** Picture Saver (round-trips a set back, tagged to a character) |
| `PixlStash-SearchToSet.json` | Search → curate | Semantic Search results saved into a chosen target Set via Picture Saver |
| `PixlStash-FaceLikenessGate.json` | Face Likeness Gate | Character Loader + image batch → Face Likeness Gate → Preview (accepted) **and** Preview (rejected) |
| `PixlStash-FaceLikenessGate-Upscale.json` | Face Likeness Gate end-to-end (T2I + upscale) | T2I (Z-Image Turbo + character LoRA) → Face Likeness Gate → upscale accepted → Picture Saver (accepted set) **and** Picture Saver (rejected set) |
| `PixlStash-Flux2-Models.json` | Checkpoint / VAE / CLIP Loaders | Checkpoint + VAE + CLIP Loader (all off the shelf) → `Text to Image (Flux.2 Klein 9B)` subgraph → Picture Saver, with a Project Loader for the target |

Notes:
- **SemanticSearch** widgets are `query`, `limit`, `threshold`. Lower the threshold (e.g. 0.3) if a query returns nothing.
- **LikenessSearch** widgets are `search_mode` (`picture_likeness` / `face_search`), `combine`, `pool_size`, `select_count`, `threshold`. `face_search` needs a query image containing a face wired into the `image` input.
- **FaceLikenessGate** widgets are `threshold`, `cleanup`, `face_timeout`. Wire a Character Loader into `pixlstash_character` and a batch of images into `image`. In the example the batch comes from a Picture Loader — fill in its `picture_ids` (or swap it for your generation pipeline, e.g. a KSampler/VAEDecode) to filter renders. It needs a **write**-scope token and a running face-extraction worker.
- **FaceLikenessGate-Upscale** is the same gate wired into a full pipeline: a Z-Image Turbo text-to-image generate (with a character LoRA) feeds the gate, accepted frames are upscaled and saved to one set, rejected frames to another. The shipped LoRA name, prompt, character, project and target sets are placeholders — point them at your own.
- **Flux2-Models** shows the three model loaders together. Flux.2 Klein 9B is a *bare diffusion model*, which the shelf files as a checkpoint on its parameter count — so only the Checkpoint Loader's `model` output is wired, and `clip` and `vae` come from their own loaders (an all-in-one checkpoint would carry all three). The CLIP Loader is on `flux2` with one encoder and an empty second slot; a model needing a pair fills both. The shelf selections are this vault's — click each **Browse** button and pick your own.
- The **model shelf loaders** need PixlStash **1.10** and an **owner** token: the shelf routes are owner-only and library-pinned, unlike the picture routes, so a resource-scoped share token is refused there even though it works everywhere else. The Checkpoint Loader additionally needs the file to be readable on the ComfyUI machine — PixlStash serves adapter bytes but not checkpoint bytes.
- The Picture Saver and Face Likeness Gate require a token with **write** scope; the search/loader nodes only need read scope.
