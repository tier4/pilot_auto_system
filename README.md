## TIER IV universe / split mirror (system)

This repository split mirrors the system subtrees of several upstream repositories, used for TIER IV release workflows.

This branch only holds the mirror configuration and its tooling. See the mirror branches for source code.

### Published branches

| Branch | Contents |
| --- | --- |
| `awf-latest-no-adapi` | `autowarefoundation/autoware_universe:main`, `system/` without default ADAPI packages |
| `awf-latest` | legacy tip (full `system/`, including default ADAPI); no longer updated by this config |
| `feat/v0.64/e2e` | `tier4/autoware_universe:feat/v0.64/e2e`, the same paths as the previous full universe mirror |

`awf-latest-no-adapi` drops packages that are consumed from `autoware_api` instead:

- `system/autoware_default_adapi_universe`
- `system/autoware_default_adapi_helpers/autoware_adapi_visualizers`
- `system/autoware_default_adapi_helpers/autoware_automatic_pose_initializer`

Changing the path filter rewrites history, so this could not fast-forward the
existing `awf-latest` tip (force-push is forbidden). The legacy branch remains
as a frozen reference.

### How it works

[`.sync/sources.yaml`](.sync/sources.yaml) is the single source of truth. It
describes every upstream, the paths to retain, the commit-message rewriting and
the branch each mirror is published to. `.github/workflows/mirror.yaml` derives
its job matrices from that file, so adding or changing a mirror is a
configuration change and never a workflow change.

`tools/mirror.py mirror SOURCE` clones the upstream and runs `git-filter-repo`
with arguments generated from the configuration, then pushes the result to that
source's `mirror_branch`.

### Determinism and publishing

Per-source mirrors are pure functions of `(upstream commit, .sync/sources.yaml)`:

- `git-filter-repo` rewrites a given history the same way every time. The
  version is pinned in the workflow, because a different version may rewrite
  differently.
- The same upstream tip therefore republishes as a fast-forward. A failed push
  means reproducibility was lost.

Publishing is **fast-forward only** (never `--force`). Mirror tips do not
require a PR; force-push and branch deletion remain blocked.

### Working on the configuration

```bash
python3 -m pip install pyyaml 'git-filter-repo==2.47.0'

tools/sync_config.py validate                # check the configuration
tools/sync_config.py show autoware_universe  # the git-filter-repo call it implies
```

Without `--push`, `tools/mirror.py mirror` is a dry run.

### Manual sync

```bash
# All sources
PUSH=1 ./tools/manual_sync.sh

# awf-latest-no-adapi only
SOURCES=autoware_universe PUSH=1 ./tools/manual_sync.sh
```
