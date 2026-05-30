# PixlStash example workflows

Drop these `.json` files onto the ComfyUI canvas (or use **Workflow → Open**). They use only PixlStash nodes plus core ComfyUI nodes (`LoadImage`, `PreviewImage`), so they load on any install once the PixlStash extension is present.

Before running, set your vault **URL** and **API Token** under **Settings → PixlStash**. The dropdowns on the loader nodes populate live from your vault, so pick your own project / set / character after loading — the saved selections are placeholders.

| File | Showcases | Flow |
|---|---|---|
| `PixlStash-SemanticSearch.json` | Semantic Search | Project Loader → Semantic Search (text query) → Preview |
| `PixlStash-LikenessSearch.json` | Likeness Search (face mode) | Load Image (query face) + Project Loader → Likeness Search → Preview |
| `PixlStash-CurateRoundtrip.json` | Loaders + Picture Loader + Picture Saver | Project → Set / Character → Picture Loader → Preview **and** Picture Saver (round-trips a set back, tagged to a character) |
| `PixlStash-SearchToSet.json` | Search → curate | Semantic Search results saved into a chosen target Set via Picture Saver |

Notes:
- **SemanticSearch** widgets are `query`, `limit`, `threshold`. Lower the threshold (e.g. 0.3) if a query returns nothing.
- **LikenessSearch** widgets are `search_mode` (`picture_likeness` / `face_search`), `combine`, `pool_size`, `select_count`, `threshold`. `face_search` needs a query image containing a face wired into the `image` input.
- The Picture Saver requires a token with **write** scope; the search/loader nodes only need read scope.
