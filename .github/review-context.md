# Review context for the AI reviewer

ComfyUI-PixlStash: a ComfyUI custom node package that talks to a PixlStash
server. Python nodes (`nodes/`), one shared HTTP client (`connection.py`),
aiohttp routes on ComfyUI's own server (`proxy_routes.py`, `serve_routes.py`)
and plain-JS frontend extensions with no build step (`web/js/`). Loaded by
`.github/workflows/ai-review.yml` from `main`.

## How to review

- Report only defects you can point at in the diff: a wrong result, a crash, a
  data-loss or security hole, or a broken rule below. Say what input breaks it.
- Skip style, naming, formatting and lint; ruff covers them. Skip speculative
  "consider adding" advice and do not restate what the diff does.
- Few findings beat many. No findings is a valid review.

## Not enforced anywhere else

This repository has no CI: the tests and ruff are run by hand. Nothing below is
caught by a machine before merge, so a broken rule here is worth reporting.

## Security rules to check

- **The token.** The PixlStash API token lives in ComfyUI Settings and travels
  only in an `Authorization: Bearer` header. A token declared in a node's
  `INPUT_TYPES`, or one that reaches the prompt, the saved workflow, a log
  line, an error message, a URL or a response body, is a finding.
- **Where the proxy points.** The proxy routes take the server URL and the SSL
  setting from ComfyUI's settings (`read_credentials`), never from the request.
  A route that builds its target from a query parameter, a header or a request
  body is a finding (SSRF).
- **What goes into an upstream path.** In `proxy_routes.py`, a value from the
  request is validated before it is interpolated into a PixlStash path:
  `_positive_id` for a row id, `_SHA256_RE` (`SHA256_RE` in `nodes/lock.py`)
  for a digest, `_WORKFLOW_ID_RE` for a workflow id. A route that forwards an
  unvalidated path segment is a finding.
- **The served routes are an auth boundary.** Every handler in
  `serve_routes.py` calls `_refusal` before doing anything else, and the token
  comparison stays `hmac.compare_digest`. A filename from the wire goes through
  `_asset_name`; loosening its whitelist, its suffix list or `MAX_ASSET_BYTES`
  is a finding unless the diff says why it is safe.
- **Paths from the network.** The Saver hands the raw filename prefix to
  `folder_paths.get_save_image_path` and writes only under the folder that
  returns; joining the prefix itself is a finding. A shelf loader uses a local
  copy only through `local_path` in `nodes/shelf_file.py`: the location was
  last seen `present`, the relative path stays inside its own folder, the
  suffix is an allowed one and the size equals the recorded size. A record
  with no usable size is refused. Loosening any of these is a finding.
- **Downloads.** `cached_download` puts nothing under `<sha256><suffix>` until
  the digest of what was downloaded matches. A path that leaves an unverified
  file under that name is a finding.
- **Uploaded assets** are never replaced: a name already in use is a 409.
- **Multi-user.** Every node and route that reads the token or reaches
  PixlStash refuses under `--multi-user`, through `read_credentials` or
  `multi_user_active`. A new one without that check is a finding.
- **TLS.** Verification follows the user's setting. A hard-coded
  `verify=False`, or a path that trusts a certificate from a folder other users
  can write, is a finding.
- **Server-supplied strings in the page.** Names and labels from PixlStash go
  into the DOM as `textContent` (the `el` helper in `web/js/modal_dom.js`),
  never through `innerHTML` or a template string of markup. Static markup with
  no server value in it is fine.

## Other rules to check

- **Siblings.** A fix to one node, route or picker that leaves identical
  siblings unfixed is a finding; name them.
- **Saved workflows.** A node's key in `NODE_CLASS_MAPPINGS`, its input names
  and the order of its `RETURN_TYPES` are stored in users' workflows. Renaming
  or reordering one breaks every saved workflow that uses it and is a finding
  unless the diff migrates them. `RETURN_TYPES`, `RETURN_NAMES` and `FUNCTION`
  must agree with each other and with the widget names the JS looks up.
- **Server version.** Every node has an entry in `NODE_MIN_VERSION` in
  `web/js/combo_widgets.js` naming the PixlStash version its routes first
  shipped in; a new node without one, or a node that starts calling a newer
  route without raising its entry, is a finding. That table only warns in the
  browser. New code that needs more than `connection.MIN_SERVER_VERSION` also
  passes the floor as `min_server_version` (to `make_client` or
  `shelf_file.client_for`), as the shelf loaders do.
- **The resolution lock.** The payload built in `nodes/lock.py` is read by
  PixlStash out of ComfyUI's history, so its shape is a contract. It reports
  what a run actually loaded, not what was asked for: a picture that was
  skipped must not appear in it.
- **Version.** `VERSION` in `connection.py` equals `version` in
  `pyproject.toml`; a diff that changes one and not the other is a finding.
- **Settings ids.** The server URL, API token and Verify SSL setting ids are
  spelled the same in `web/js/` and in the `_SETTING_*` constants in
  `connection.py`.
- **Vendored code.** The upstream files under `vendor/comfyui_gguf/` change
  only as whole-file copies in a refresh, with the commit in `VENDORED.md`
  updated. A hand edit to one is a finding. A refresh that changes upstream's
  `load_unet` or `load_data` without carrying the change into
  `nodes/gguf_support.py`, which copies their bodies, is a finding. Only
  `nodes/gguf_support.py` imports from there.
- **Imports.** `connection.py`, the route modules and the shelf loaders import
  `folder_paths`, `comfy.*` and `server` inside the function that needs them,
  so they load without ComfyUI; moving one to the top of those files is a
  finding. `gguf` is optional and imported on first use. A new dependency is
  listed in both `requirements.txt` and `pyproject.toml`; what ComfyUI already
  provides (torch, numpy, aiohttp) is listed in neither.
- **The event loop.** A route handler never makes a PixlStash request on the
  event loop; calls on the client go through `asyncio.to_thread`.
- **Errors.** A failure the user will see is a `RuntimeError` that names
  PixlStash or the node and says what went wrong and what to do. Skipping one
  picture that failed to load, or falling back to another route, is deliberate
  where it exists: the exception is logged with what was skipped, and the run
  still fails when nothing usable is left. A caught exception that is neither
  logged nor re-raised, or one that lets a failed save or a digest mismatch
  report success, is a finding.
- **Tests** are stdlib `unittest`, most of them over the stubs in
  `tests/_bootstrap.py`, and prove only what the stubs model. The gate in
  `.github/workflows/ai-review.yml` that decides who may spend the review key
  is pinned by `tests/test_ai_review_gate.py`; a change to one without the
  other is a finding. A test that
  would still pass with the fix reverted, or that asserts a traversal was
  refused by checking an unrelated directory is empty, is a finding. A change
  to a security rule above without a test in both directions (the bad input
  refused, the good one still accepted) is a finding.

## Repository rules

- No real home paths, host names, tokens or addresses in code, tests or docs.
