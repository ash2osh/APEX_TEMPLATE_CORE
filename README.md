# APEX template core

One source for the shared `scripts/`, `tools/` and `tests/` files of the two APEX team templates
(26.1 and 26.2). `sync_core.py` copies them into each template at their existing paths, so
downstream projects and template imports see no change.

## What is here

- `files/`: the shared files, byte-identical to both templates at the seeded commits. The checker is
  included as `files/scripts/sync_core.py`.
- `core-manifest.json`: `schemaVersion` 1, the source commits (`sources`), and `entries` sorted by path,
  each with `path`, `sha256` and `executable`.
- `sync_core.py`: the commands `seed`, `refresh`, `sync`, `check` and `diff`. Python 3.10+, standard library only.
- `tests/test_sync_core.py`: unit tests. They build temporary git repositories and never touch a real template.

## Phase 1 scope

- Seeded from 26.1 `main` (`6c3158e`) and 26.2 `origin/main` (`d9e26a2`).
- Selection: paths under `scripts/`, `tools/` and `tests/` whose blob and executable bit are identical in both templates.
  That is 199 files, plus `scripts/sync_core.py` (core-owned, copied from the core root) = 200 entries.
- Not in the core, so they stay per template:
  - 105 differing files (52 in `scripts/` and `tools/`, 53 in `tests/`).
  - 110 one-sided files (2 only in 26.1, 108 only in 26.2).

### Exclusions

| Path | Reason |
| --- | --- |
| `scripts/ci_run_tests.py` | Template CI runner. Only the template's GitHub workflow calls it, and downstream projects remove `.github`. |
| `tests/test_ci_run_tests.py` | Tests that runner. Excluded with it so the pair stays together. |

The downstream cleanup also removes all of `tests/`. These tests stay in the core because the templates'
own CI runs them against the shared scripts. Whether `tests/` belongs in Phase 2 is an open decision.

## Workflow

1. Fix in the core. Edit the file under `files/` (or `sync_core.py` at the root), then run
   `python3 sync_core.py refresh`. It re-hashes `files/` into the manifest and re-copies the checker. It adds and removes no paths.
2. Test: `python3 -m unittest discover -s tests -v` and `ruff check sync_core.py tests/`.
3. Commit in this repository. `sync` refuses a core with uncommitted changes.
4. Sync into each template, on a branch in that template:
   `python3 /path/to/APEX_TEMPLATE_CORE/sync_core.py sync /path/to/template --core /path/to/APEX_TEMPLATE_CORE`.
   Review `git status` and `git diff` in the template, commit, and open one pull request per template.
5. Preview without writing: `python3 sync_core.py diff /path/to/template --core /path/to/APEX_TEMPLATE_CORE`.
   Exit 1 means something would change or is blocked.
6. Rebuild from the templates only when their committed content changes:
   ```
   python3 sync_core.py seed \
     --template 26.1=/path/to/APEX_PROJECT_TEMPLATE_26_1@main \
     --template 26.2=/path/to/APEX_PROJECT_TEMPLATE_TEAM@origin/main \
     --prefix scripts/ --prefix tools/ --prefix tests/ \
     --exclude scripts/ci_run_tests.py --exclude tests/test_ci_run_tests.py
   ```
   `seed` rebuilds `files/` from the templates, so it discards any hand fix that was not also made in a template.

`sync` refuses (exit 1, nothing written) when a target path has uncommitted or untracked changes in the template,
when a symbolic link sits on a target path, when a target path resolves outside the template, or when a manifest path would leave the template.
After each sync, commit the changed files and `.core-stamp.json` before the next sync. A path that is no longer in
the core is reported as stale and left in place.

Exit codes for every command: 0 ok; 1 drift found, or refused before any write; 2 usage or internal error.

## How CI uses `check` (planned, not yet in the templates)

- `sync` writes `.core-stamp.json` into the template: the core commit id, plus `path`, `sha256` and
  `executable` for each file.
- Template CI will run `python3 scripts/sync_core.py check .` on each pull request. It is offline, needs no core
  checkout, and exits 1 listing each file whose bytes differ from the stamp or that is missing.
- `check . --core <core checkout>` also compares the stamp with `core-manifest.json` at the stamped commit. Use it only
  in a job that checks out the core.
- `check` compares bytes, not the executable bit. Both templates' `.gitattributes` force LF on these file types, so a
  Windows checkout has the same bytes as Linux and `check` is valid there too.

## Next phases

- Phase 2: merge the differing files one at a time into the core, behind an `apexRelease` switch, so 26.1 and 26.2
  keep their current behaviour. Decide first whether the 53 differing `tests/` files are in scope.
- Integration: after the current template fix branches merge, add `.core-stamp.json` through `sync` and the `check` CI
  step to each template. The template copy of `sync_core.py` arrives with the first sync.

## Known limits

- `sync` is atomic per file, not across the set. An interrupted run leaves some files written. The stamp is written last,
  so `check` reports drift until `sync` is re-run and completes.
- This phase was run and tested on Python 3.14 only. The code targets 3.10 and passes `ruff` with `target-version = "py310"`.
