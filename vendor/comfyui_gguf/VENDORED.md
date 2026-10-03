# Vendored: ComfyUI-GGUF

- Upstream: https://github.com/city96/ComfyUI-GGUF
- Commit: `6ea2651e7df66d7585f6ffee804b20e92fb38b8a` (2026-01-12)
- Licence: Apache-2.0, see `LICENSE`

`nodes.py`, `loader.py`, `ops.py`, `dequant.py`, `tools/convert.py` and
`LICENSE` are upstream's files, **unmodified — never edit them**. `loader.py`
imports `tools/convert.py` to identify a GGUF that carries no architecture
tag (one made by stable-diffusion.cpp, say). `__init__.py` is ours and empty on
purpose: upstream's registers its nodes, and this pack must not.

Only `nodes/gguf_support.py` imports from here.

## Refreshing

```sh
UPSTREAM=/path/to/ComfyUI-GGUF   # a checkout of the commit you want
for f in nodes.py loader.py ops.py dequant.py tools/convert.py LICENSE; do cp "$UPSTREAM/$f" "vendor/comfyui_gguf/$f"; done
```

Then update the commit above, and diff upstream's `UnetLoaderGGUF.load_unet`
against the previous pin: `gguf_support.load_unet` copies its body (upstream
takes a filename, we have a path), so any change there has to be carried
across by hand.
