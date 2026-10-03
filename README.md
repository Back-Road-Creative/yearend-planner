# Year-End Tax & Retirement Planner

A Windows program that reads the tax documents you drop into a folder, computes your
taxes with [PolicyEngine US](https://github.com/PolicyEngine/policyengine-us), shows what
is still missing, surfaces every tax lever, and at filing time hands you a complete tax
package. Deterministic, local, no account, no cloud, no LLM at runtime.

Status: Phase 0 (foundations). See the build plan for the phases.

## Use (Windows)

1. Download `yearend-planner-<version>-win64.zip`, right-click → Properties → **Unblock**.
2. Extract it anywhere local (not OneDrive, Dropbox or another synced folder).
3. Double-click `planner.cmd`.

Everything personal stays under `data/` and `out/` inside that folder.

## Develop

```
uv sync --frozen --extra dev
uv run pytest            # add -m "not engine" to skip the ~1 min engine case
uv run ruff check . && uv run ruff format --check . && uv run mypy planner tests
uv run --no-project python scripts/build_release.py   # dist/yearend-planner-<v>-win64.zip
```

CI runs the suite on Ubuntu and Windows and proves the release zip on a clean Windows
runner (`scripts/windows_proof.ps1`): real calculation, path with a space and non-ASCII
characters, moved folder, network blocked, standard user, cloud-sync folder refused.

## License

AGPL-3.0-or-later. PolicyEngine US is AGPL-3.0; this planner is distributed under the
same terms.
