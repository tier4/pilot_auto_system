#!/usr/bin/env python3
"""Replay several filtered histories into one deterministic linear branch.

Each member history occupies a disjoint set of top-level paths. Every commit is
replayed in its original form -- same author, committer, timestamps and message
-- with only two things rewritten: its parent becomes the previously replayed
commit, and its tree is recomposed from the latest state of every member.

Nothing is synthesised. No merge commit is created and no wall-clock value ever
reaches an object, so the resulting branch is a pure function of the member tips
and the ordering rule. Rebuilding from scratch reproduces the same commit ids.

Trees are carried whole rather than as deltas, so merge commits inside a member
history linearise without losing content.
"""

from __future__ import annotations

import heapq
import subprocess
from dataclasses import dataclass

# git stores directory entries as "40000"; fast-import wants the padded form.
_TREE_MODE = b"40000"
_TREE_MODE_PADDED = b"040000"


class CombineError(Exception):
    """Raised when the member histories cannot be replayed into one branch."""


@dataclass
class MemberSpec:
    name: str
    url: str
    ref: str
    rename: dict[str, str]


@dataclass
class _Commit:
    oid: bytes
    tree: bytes
    author: bytes
    committer: bytes
    message: bytes
    order_key: int


def _git(repo: str, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise CombineError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _batch_read(repo: str, oids: list[bytes]) -> dict[bytes, bytes]:
    """Read many objects with a single `git cat-file --batch` invocation."""
    if not oids:
        return {}
    raw = _git(repo, "cat-file", "--batch", stdin=b"\n".join(oids) + b"\n")
    contents: dict[bytes, bytes] = {}
    position = 0
    end = len(raw)
    while position < end:
        newline = raw.index(b"\n", position)
        header = raw[position:newline].split(b" ")
        if len(header) != 3:
            raise CombineError(f"unexpected cat-file header: {raw[position:newline]!r}")
        oid, _, size = header
        start = newline + 1
        length = int(size)
        contents[oid] = raw[start : start + length]
        position = start + length + 1
    return contents


def _parse_commit_object(raw: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    """Split a raw commit object into (tree, author, committer, message).

    Any signature header is dropped, which matches `fast-export
    --signed-commits=strip`: a signature over the original parent would be void
    after reparenting anyway.
    """
    header, _, message = raw.partition(b"\n\n")
    tree = author = committer = None
    for line in header.split(b"\n"):
        if line.startswith(b" "):
            continue  # continuation of a folded header such as gpgsig
        if line.startswith(b"tree "):
            tree = line[len(b"tree ") :]
        elif line.startswith(b"author "):
            author = line[len(b"author ") :]
        elif line.startswith(b"committer "):
            committer = line[len(b"committer ") :]
    if tree is None or author is None or committer is None:
        raise CombineError("commit object is missing tree, author or committer")
    return tree, author, committer, message


def _parse_tree_object(raw: bytes) -> list[tuple[bytes, bytes, bytes]]:
    """Return the (mode, name, hex oid) entries of a tree object."""
    entries = []
    position = 0
    end = len(raw)
    while position < end:
        space = raw.index(b" ", position)
        mode = raw[position:space]
        nul = raw.index(b"\x00", space)
        name = raw[space + 1 : nul]
        oid = raw[nul + 1 : nul + 21]
        if mode == _TREE_MODE:
            mode = _TREE_MODE_PADDED
        entries.append((mode, name, oid.hex().encode()))
        position = nul + 21
    return entries


def _timestamp(identity: bytes) -> int:
    """Extract the epoch out of an `author`/`committer` header value."""
    try:
        return int(identity.rsplit(b" ", 2)[-2])
    except (IndexError, ValueError) as exc:
        raise CombineError(f"cannot read a timestamp from {identity!r}") from exc


def _quote_path(name: bytes) -> bytes:
    if not name or name.startswith(b'"') or any(byte in name for byte in b'"\n\\'):
        escaped = name.replace(b"\\", b"\\\\").replace(b'"', b'\\"').replace(b"\n", b"\\n")
        return b'"' + escaped + b'"'
    return name


def _load_member(repo: str, ref: str, order_by: str) -> list[_Commit]:
    listing = _git(repo, "rev-list", "--topo-order", "--reverse", ref)
    oids = listing.split()
    if not oids:
        raise CombineError(f"{ref} has no commits")
    objects = _batch_read(repo, oids)
    commits = []
    for oid in oids:
        tree, author, committer, message = _parse_commit_object(objects[oid])
        identity = committer if order_by == "committer_date" else author
        commits.append(
            _Commit(
                oid=oid,
                tree=tree,
                author=author,
                committer=committer,
                message=message,
                order_key=_timestamp(identity),
            )
        )
    return commits


def _top_level_state(
    repo: str, commits: list[_Commit], rename: dict[str, str]
) -> list[list[bytes]]:
    """Pre-render the fast-import file-change lines for every commit of a member."""
    trees = _batch_read(repo, [commit.tree for commit in commits])
    mapping = {key.encode(): value.encode() for key, value in rename.items()}
    rendered = []
    for commit in commits:
        lines = []
        taken: set[bytes] = set()
        for mode, name, oid in _parse_tree_object(trees[commit.tree]):
            target = mapping.get(name, name)
            if target in taken:
                # Two entries landing on one name would silently drop whichever
                # was written first, so refuse instead.
                raise CombineError(
                    f"rename maps more than one top-level path onto "
                    f"{target.decode(errors='replace')!r} in {commit.oid.decode()}"
                )
            taken.add(target)
            lines.append(b"M " + mode + b" " + oid + b" " + _quote_path(target))
        rendered.append(lines)
    return rendered


def _member_names(entries: list[bytes]) -> set[bytes]:
    return {line.rsplit(b" ", 1)[-1] for line in entries}


def combine(repo: str, branch: str, members: list[MemberSpec], order_by: str) -> str:
    """Build `branch` inside `repo` by replaying `members`, and return its oid."""
    if len(members) < 2:
        raise CombineError("a combined branch needs at least two members")

    histories: list[list[_Commit]] = []
    states: list[list[list[bytes]]] = []
    occupied: list[set[bytes]] = []
    for index, member in enumerate(members):
        target = f"refs/mirror-members/{member.name}"
        _git(repo, "fetch", "--quiet", "--no-tags", "--force", member.url, f"{member.ref}:{target}")
        commits = _load_member(repo, target, order_by)
        histories.append(commits)
        states.append(_top_level_state(repo, commits, member.rename))
        # Every path the member ever occupies, not just the ones it ends with:
        # a collision anywhere in the history would corrupt the replay.
        occupied.append(set().union(*(_member_names(entry) for entry in states[index])))
        print(f"  {member.name}: {len(commits)} commit(s) from {member.ref}")
        for other in range(index):
            overlap = occupied[other] & occupied[index]
            if overlap:
                names = b", ".join(sorted(overlap)).decode(errors="replace")
                raise CombineError(
                    f"{members[other].name} and {member.name} both provide "
                    f"top-level path(s): {names}"
                )

    # A k-way merge over lists that are already in dependency order preserves
    # each member's internal order whatever the timestamps do, and the
    # (key, member, oid) tuple makes the total order stable.
    queue = [
        (history[0].order_key, index, history[0].oid, 0)
        for index, history in enumerate(histories)
        if history
    ]
    heapq.heapify(queue)

    stream = bytearray()
    current: list[list[bytes] | None] = [None] * len(members)
    total = sum(len(history) for history in histories)
    ref = f"refs/heads/{branch}".encode()
    mark = 0
    while queue:
        _, member_index, _, position = heapq.heappop(queue)
        commit = histories[member_index][position]
        current[member_index] = states[member_index][position]
        mark += 1

        stream += b"commit " + ref + b"\n"
        stream += b"mark :" + str(mark).encode() + b"\n"
        stream += b"original-oid " + commit.oid + b"\n"
        stream += b"author " + commit.author + b"\n"
        stream += b"committer " + commit.committer + b"\n"
        stream += b"data " + str(len(commit.message)).encode() + b"\n"
        stream += commit.message
        if mark > 1:
            stream += b"from :" + str(mark - 1).encode() + b"\n"
        stream += b"deleteall\n"
        for entries in current:
            if entries:
                stream += b"\n".join(entries) + b"\n"

        following = position + 1
        if following < len(histories[member_index]):
            nxt = histories[member_index][following]
            heapq.heappush(queue, (nxt.order_key, member_index, nxt.oid, following))

    stream += b"done\n"
    print(f"  replaying {total} commit(s) into {branch}")
    _git(repo, "fast-import", "--quiet", "--force", "--done", stdin=bytes(stream))
    return _git(repo, "rev-parse", f"refs/heads/{branch}").decode().strip()
