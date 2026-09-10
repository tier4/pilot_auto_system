#!/usr/bin/env python3
"""Publish every mirror described by .sync/sources.yaml.

    mirror.py list-sources                  names of the per-source mirrors
    mirror.py list-combined                 names of the combined branches
    mirror.py mirror SOURCE --work DIR      filter one upstream, optionally push
    mirror.py combine TARGET --work DIR     replay mirror branches into one branch

`mirror` clones the upstream and runs git-filter-repo with arguments derived
from the configuration. `combine` reads the already published mirror branches,
so it never needs an upstream clone and cannot disagree with what was pushed.

Both stages are deterministic. `--verify` re-runs a combine from scratch into a
second repository and fails unless the two builds agree, which turns the
determinism contract in sources.yaml into something CI actually checks.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

import combine_history
import sync_config
from combine_history import CombineError, MemberSpec
from sync_config import ConfigError


def _run(args: list[str], cwd: str | None = None) -> None:
    result = subprocess.run(args, cwd=cwd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}")


def _capture(args: list[str], cwd: str | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _fresh_dir(path: str) -> str:
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    return path


def _push(repo: str, downstream: str, local_ref: str, branch: str, force: bool) -> None:
    args = ["git", "-C", repo, "push"]
    if force:
        args.append("--force")
    args += [downstream, f"{local_ref}:refs/heads/{branch}"]
    _run(args)


def _report_reproducibility(repo: str, downstream: str, branch: str, built: str) -> None:
    """Say whether the freshly built branch still contains what we published.

    A fast-forward means every previously published commit id came out the same,
    which is the determinism contract holding in production. Anything else is a
    rewrite, and worth shouting about even when the push is allowed to force.
    """
    ref = f"refs/mirror-published/{branch}"
    probe = subprocess.run(
        [
            "git",
            "-C",
            repo,
            "fetch",
            "--quiet",
            "--no-tags",
            "--force",
            downstream,
            f"refs/heads/{branch}:{ref}",
        ],
        check=False,
        capture_output=True,
    )
    if probe.returncode != 0:
        print(f"  {branch}: not published yet, nothing to compare against")
        return
    published = _capture(["git", "-C", repo, "rev-parse", ref])
    if published == built:
        print(f"  {branch}: unchanged ({built[:12]})")
        return
    ancestor = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", published, built],
        check=False,
    )
    if ancestor.returncode == 0:
        print(f"  {branch}: fast-forward from {published[:12]} to {built[:12]}")
    else:
        print(
            f"  {branch}: WARNING history rewritten, {published[:12]} is not an "
            f"ancestor of {built[:12]}; previously published commit ids changed"
        )


def do_mirror(config: sync_config.Config, args: argparse.Namespace) -> int:
    source = config.sources.get(args.source)
    if source is None:
        raise ConfigError(f"unknown source {args.source!r}")

    work = _fresh_dir(os.path.join(args.work, source.name))
    clone = os.path.join(work, "upstream")
    print(f"cloning {source.upstream} ({source.ref})")
    _run(
        [
            "git",
            "clone",
            "--quiet",
            "--single-branch",
            "--branch",
            source.ref,
            source.upstream,
            clone,
        ]
    )

    filter_args = sync_config.filter_repo_args(source, work)
    print(f"filtering {source.name}: git filter-repo {' '.join(filter_args)}")
    _run(["git", "filter-repo", *filter_args], cwd=clone)

    tip = _capture(["git", "-C", clone, "rev-parse", "HEAD"])
    count = _capture(["git", "-C", clone, "rev-list", "--count", "HEAD"])
    print(f"  {source.name}: {count} commit(s), tip {tip[:12]}")

    if not source.mirror_branch:
        print(f"  {source.name}: no mirror_branch configured, not pushing")
        return 0
    if args.downstream:
        _report_reproducibility(clone, args.downstream, source.mirror_branch, tip)
    if args.push:
        if not args.downstream:
            raise RuntimeError("--push needs --downstream")
        _push(clone, args.downstream, "HEAD", source.mirror_branch, source.force)
        print(f"  pushed {source.mirror_branch}")
    else:
        print("  dry run, nothing pushed")
    return 0


def _remote_has_branch(downstream: str, branch: str) -> bool:
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", downstream, f"refs/heads/{branch}"],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _build_combined(config: sync_config.Config, name: str, repo: str, downstream: str) -> str:
    target = config.combined[name]
    members = [
        MemberSpec(
            name=member.source,
            url=downstream,
            ref=f"refs/heads/{config.sources[member.source].mirror_branch}",
            rename=member.rename,
        )
        for member in target.members
    ]
    _run(["git", "init", "--quiet", repo])
    return combine_history.combine(repo, name, members, target.order_by)


def do_combine(config: sync_config.Config, args: argparse.Namespace) -> int:
    if args.target not in config.combined:
        raise ConfigError(f"unknown combined target {args.target!r}")
    if not args.downstream:
        raise RuntimeError("combine needs --downstream to read the mirror branches")

    missing = [
        config.sources[member.source].mirror_branch
        for member in config.combined[args.target].members
        if not _remote_has_branch(
            args.downstream, config.sources[member.source].mirror_branch or ""
        )
    ]
    if missing:
        message = f"{args.target}: member branch(es) not published yet: {', '.join(missing)}"
        if not args.allow_missing_members:
            raise RuntimeError(message)
        print(f"{message}; skipping")
        return 0

    work = _fresh_dir(os.path.join(args.work, args.target))
    repo = os.path.join(work, "combined")
    print(f"building {args.target}")
    built = _build_combined(config, args.target, repo, args.downstream)
    print(f"  {args.target}: tip {built[:12]}")

    if args.verify:
        second = os.path.join(work, "verify")
        print(f"verifying {args.target} by rebuilding from scratch")
        again = _build_combined(config, args.target, second, args.downstream)
        if again != built:
            raise RuntimeError(
                f"determinism check failed: {built} on the first build, {again} on the second"
            )
        print(f"  {args.target}: reproducible, both builds are {built[:12]}")

    _report_reproducibility(repo, args.downstream, args.target, built)

    if args.push:
        _push(
            repo,
            args.downstream,
            f"refs/heads/{args.target}",
            args.target,
            config.combined[args.target].force,
        )
        print(f"  pushed {args.target}")
    else:
        print("  dry run, nothing pushed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=sync_config.DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-sources")
    sub.add_parser("list-combined")

    mirror = sub.add_parser("mirror")
    mirror.add_argument("source")
    mirror.add_argument("--work", required=True)
    mirror.add_argument("--downstream")
    mirror.add_argument("--push", action="store_true")

    combine = sub.add_parser("combine")
    combine.add_argument("target")
    combine.add_argument("--work", required=True)
    combine.add_argument("--downstream")
    combine.add_argument("--push", action="store_true")
    combine.add_argument("--verify", action="store_true")
    combine.add_argument(
        "--allow-missing-members",
        action="store_true",
        help="skip the target instead of failing when a member branch is not published yet",
    )

    args = parser.parse_args()
    config = sync_config.load(args.config)

    if args.command == "list-sources":
        print(json.dumps(sorted(config.sources)))
        return 0
    if args.command == "list-combined":
        print(json.dumps(sorted(config.combined)))
        return 0
    if args.command == "mirror":
        return do_mirror(config, args)
    return do_combine(config, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ConfigError, CombineError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
