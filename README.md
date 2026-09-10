## TIER IV universe / split mirror (system)

This repository split mirrors the system subtrees of several upstream repositories, used for TIER IV release workflows.

This branch only holds the mirror configuration and its tooling. See the mirror branches for source code.

### Published branches

| Branch | Contents |
| --- | --- |
| `awf-latest/universe` | `autowarefoundation/autoware_universe:main`, `system/` |
| `feat/v0.64/e2e` | `tier4/autoware_universe:feat/v0.64/e2e`, the same paths as the universe mirror |

The upstream mirrors live under the `awf-latest/` namespace. Note that git cannot
hold a branch named `awf-latest` at the same time as `awf-latest/*`, so the old
flat branch has to be deleted before these can be created.

### How it works

[`.sync/sources.yaml`](.sync/sources.yaml) is the single source of truth. It
describes every upstream, the paths to retain, the commit-message rewriting and
the branch each mirror is published to. `.github/workflows/mirror.yaml` derives
its job matrices from that file, so adding or changing a mirror is a
configuration change and never a workflow change.

The pipeline has two stages:

1. **Filter.** `tools/mirror.py mirror SOURCE` clones the upstream and runs
   `git-filter-repo` with arguments generated from the configuration, then
   pushes the result to that source's `mirror_branch`.
2. **Combine.** `tools/mirror.py combine TARGET` reads the mirror branches that
   stage 1 published and replays them into a single linear history ordered by
   committer date. It never clones an upstream, so the combined branch cannot
   disagree with the per-source mirrors.

### Determinism

Both stages are pure functions of `(upstream commit, .sync/sources.yaml)`:

- `git-filter-repo` rewrites a given history the same way every time. The
  version is pinned in the workflow, because a different version may rewrite
  differently.
- The combiner synthesises nothing. Every replayed commit keeps its original
  author, committer, timestamps and message; only its parent is rewritten, and
  its tree is recomposed from the current state of each member. No wall-clock
  value ever reaches an object.

This is checked rather than assumed:

- `tools/mirror.py combine --verify` rebuilds the branch from scratch a second
  time and fails unless both builds produce the same commit id. The scheduled
  workflow always passes `--verify`.
- Every push reports whether the previously published tip is still an ancestor
  of the new one. A fast-forward means the contract held; anything else is
  reported as a rewrite.
- `awf-latest/universe` is pushed without `--force` on purpose, so losing
  reproducibility there fails the job instead of silently republishing.

### Working on the configuration

```bash
python3 -m pip install pyyaml git-filter-repo==2.47.0

tools/sync_config.py validate                # check the configuration
tools/sync_config.py show autoware_universe  # the git-filter-repo call it implies
```

Without `--push`, `tools/mirror.py mirror` and `tools/mirror.py combine` are dry runs.
