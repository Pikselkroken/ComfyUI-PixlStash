# Vendored: ComfyUI-GGUF

- Upstream: https://github.com/city96/ComfyUI-GGUF
- Pinned commit: `6ea2651e7df66d7585f6ffee804b20e92fb38b8a` (2026-01-12)
- Licence: Apache-2.0, see `LICENSE` beside this file.

`nodes.py`, `loader.py`, `ops.py`, `dequant.py` and `LICENSE` are upstream's
files, **unmodified. Never edit them.** A fix belongs upstream, or in our own
code in `nodes/gguf_support.py`, which is the only module that imports this
package.

`__init__.py` is ours and deliberately empty: upstream's registers its nodes
with ComfyUI, and this pack must not register them a second time.

## Refreshing

```sh
UPSTREAM=/path/to/a/checkout/of/ComfyUI-GGUF   # at the commit you are moving to
for f in nodes.py loader.py ops.py dequant.py LICENSE; do cp "$UPSTREAM/$f" vendor/comfyui_gguf/; done
```

Then update the pinned commit above, and **diff upstream's
`UnetLoaderGGUF.load_unet`** (in `nodes.py`) against the pinned commit. It is
the one piece of upstream logic we copy rather than call, as
`gguf_support.load_unet`, because upstream takes a filename out of ComfyUI's
folder list and we have a path. Carry any change across.

Check the copy is untouched:

```sh
for f in nodes.py loader.py ops.py dequant.py LICENSE; do
  diff <(git -C "$UPSTREAM" show 6ea2651:$f) vendor/comfyui_gguf/$f
done
```
