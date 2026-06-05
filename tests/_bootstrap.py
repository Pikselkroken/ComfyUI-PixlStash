"""Test bootstrap: import PixlStash modules without a running ComfyUI.

ComfyUI normally supplies ``folder_paths``, ``server``, ``torch``, ``numpy``,
``aiohttp`` and ``comfy.cli_args`` at runtime, and loads this repo as a package
by path.  None of that exists in the lint/test venv, so this helper:

* registers the repo as a synthetic package so the relative imports in
  ``proxy_routes.py`` / ``nodes/*`` resolve, and
* provides minimal stand-ins for the heavy/ComfyUI-only modules.

The stubs are deliberately tiny: just enough surface for the security paths
under test (credential resolution, the multi-user guard, the proxy SSRF
checks, id extraction, and the Saver's output-path containment).
"""

from __future__ import annotations

import contextlib
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = "pixlstash_under_test"


# ---------------------------------------------------------------------------
# Synthetic package loading
# ---------------------------------------------------------------------------


def _ensure_packages() -> None:
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(ROOT)]
        pkg.__package__ = PKG
        sys.modules[PKG] = pkg
    nodes = f"{PKG}.nodes"
    if nodes not in sys.modules:
        npkg = types.ModuleType(nodes)
        npkg.__path__ = [str(ROOT / "nodes")]
        npkg.__package__ = nodes
        sys.modules[nodes] = npkg


def load(modname: str):
    """Load ``<repo>/<modname>.py`` as a submodule of the synthetic package.

    ``modname`` uses dotted form, e.g. ``"connection"`` or
    ``"nodes.picture_saver"``.  Relative imports inside the module resolve
    against the synthetic package.
    """
    _ensure_packages()
    full = f"{PKG}.{modname}"
    if full in sys.modules:
        return sys.modules[full]
    path = ROOT / (modname.replace(".", "/") + ".py")
    spec = importlib.util.spec_from_file_location(full, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


@contextlib.contextmanager
def patched_modules(mods: dict):
    """Temporarily install/remove entries in ``sys.modules``.

    A value of ``None`` removes the module for the duration of the block.
    Original state is restored on exit.
    """
    saved = {name: sys.modules.get(name) for name in mods}
    try:
        for name, mod in mods.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


# ---------------------------------------------------------------------------
# Minimal stubs for ComfyUI-only / heavy modules
# ---------------------------------------------------------------------------


def cli_args_modules(multi_user: bool) -> dict:
    """sys.modules entries that make ``from comfy.cli_args import args`` work."""
    comfy = types.ModuleType("comfy")
    comfy.__path__ = []
    cli = types.ModuleType("comfy.cli_args")
    cli.args = types.SimpleNamespace(multi_user=multi_user)
    return {"comfy": comfy, "comfy.cli_args": cli}


def aiohttp_module() -> types.ModuleType:
    """A stub ``aiohttp`` exposing just ``web.HTTPBadRequest`` / ``web.Response``."""
    web = types.ModuleType("aiohttp.web")

    class HTTPBadRequest(Exception):
        def __init__(self, reason=None):
            self.reason = reason
            super().__init__(reason)

    class Request:  # only used in (stringised) annotations
        pass

    class Response:
        def __init__(self, *a, **k):
            self.kwargs = k

    web.HTTPBadRequest = HTTPBadRequest
    web.Request = Request
    web.Response = Response

    aiohttp = types.ModuleType("aiohttp")
    aiohttp.web = web
    return aiohttp


def imaging_modules(folder_paths):
    """sys.modules stubs for numpy + PIL used by picture_saver at import time."""
    numpy = types.ModuleType("numpy")
    numpy.uint8 = "uint8"

    class _FakeImage:
        def save(self, buf, **kwargs):
            buf.write(b"\x89PNG\r\n")

    pil = types.ModuleType("PIL")

    class _Image:
        Image = _FakeImage

        @staticmethod
        def fromarray(arr, mode=None):
            return _FakeImage()

    pil.Image = _Image
    pil_png = types.ModuleType("PIL.PngImagePlugin")

    class PngInfo:
        def add_text(self, key, value):
            pass

    pil_png.PngInfo = PngInfo

    return {
        "numpy": numpy,
        "PIL": pil,
        "PIL.Image": pil.Image,
        "PIL.PngImagePlugin": pil_png,
        "folder_paths": folder_paths,
    }


class FakeRequest:
    """Stand-in for aiohttp.web.Request, exposing only what's exercised."""

    def __init__(self, headers=None):
        self.headers = headers or {}


def load_proxy():
    """Load ``proxy_routes`` with a stub ``aiohttp`` installed first."""
    if "aiohttp" not in sys.modules:
        aiohttp = aiohttp_module()
        sys.modules["aiohttp"] = aiohttp
        sys.modules["aiohttp.web"] = aiohttp.web
    return load("proxy_routes")
