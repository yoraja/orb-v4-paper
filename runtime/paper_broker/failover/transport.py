"""Ownership transports.

Production transport is git compare-and-swap (``git push --force-with-lease``)
of ``ownership.json`` on branch ``ownership`` in the existing PAPER dashboard
repo. That is the same GitHub credential that already publishes heartbeat.json.
It is not a new credential, a tunnel, or a shared disk.

A per-host file is not a transport. The file backend exists only when tests
set ``ORB_V4_OWNERSHIP_FILE_IS_TEST=1``.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

OWNERSHIP_BRANCH = "ownership"
OWNERSHIP_FILE = "ownership.json"

PREREQUISITE = (
    "Cross-host ownership is git compare-and-swap on branch 'ownership' of "
    "https://github.com/yoraja/orb-v4-paper (file ownership.json), using the "
    "existing Pages push credential that already publishes heartbeat.json. "
    "Both YOPC and box must be able to fetch and push that branch. "
    "Per-host claim files and a shared filesystem are not a lock. "
    "This process fail-closed and must not submit orders until a fencing "
    "token is confirmed on that branch."
)


class TransportError(RuntimeError):
    pass


class OwnershipPrerequisite(TransportError):
    def __init__(self, detail: str = ""):
        msg = PREREQUISITE if not detail else f"{PREREQUISITE} Detail: {detail}"
        super().__init__(msg)


@dataclass
class ReadResult:
    doc: dict | None
    sha: str
    error: str | None = None


@dataclass
class WriteResult:
    status: str  # ok | rejected | ambiguous
    sha: str | None = None
    detail: str = ""


class MemoryTransport:
    """In-process CAS used by tests. Check-then-set is atomic under the GIL."""

    def __init__(self):
        self.doc: dict | None = None
        self.sha: str = ""
        self.writes = 0
        self.reads = 0
        self.mode = "ok"
        self._seq = 0

    def read(self) -> ReadResult:
        self.reads += 1
        if self.mode == "ambiguous_unreadable" and self.reads > 1:
            raise TransportError("confirm read timed out")
        if self.mode == "read_error":
            raise TransportError("ownership unreadable")
        doc = None if self.doc is None else json.loads(json.dumps(self.doc))
        return ReadResult(doc, self.sha, None)

    def cas_write(self, expected_sha: str, doc: dict) -> WriteResult:
        self.writes += 1
        if self.mode in ("ambiguous", "ambiguous_unreadable", "ambiguous_stale"):
            if self.mode == "ambiguous" and expected_sha == self.sha:
                self._commit(doc)
            return WriteResult("ambiguous", None, "timeout")
        if expected_sha != self.sha:
            return WriteResult("rejected", self.sha, "expected sha mismatch")
        self._commit(doc)
        return WriteResult("ok", self.sha, "")

    def _commit(self, doc: dict) -> None:
        self._seq += 1
        self.doc = json.loads(json.dumps(doc))
        self.sha = f"m{self._seq}"


class GitBranchTransport:
    """CAS against a git remote. Tests pass a local bare repo as ``remote``."""

    def __init__(self, remote: str, work: Path, branch: str = OWNERSHIP_BRANCH, timeout_s: float = 20.0):
        if not remote:
            raise OwnershipPrerequisite("ORB_V4_OWNERSHIP_REMOTE is unset")
        self.remote = remote
        self.work = Path(work)
        self.branch = branch
        self.timeout_s = timeout_s

    def read(self) -> ReadResult:
        self._ensure()
        fetch = self._git(["fetch", "origin", self.branch])
        if fetch.returncode != 0:
            err = (fetch.stderr or "") + (fetch.stdout or "")
            if _missing_ref(err):
                return ReadResult(None, "", None)
            raise TransportError(err[-400:] or "git fetch failed")
        sha = self._git(["rev-parse", "FETCH_HEAD"])
        if sha.returncode != 0:
            raise TransportError((sha.stderr or "")[-400:] or "rev-parse failed")
        show = self._git(["show", f"FETCH_HEAD:{OWNERSHIP_FILE}"])
        if show.returncode != 0:
            return ReadResult(None, sha.stdout.strip(), None)
        return ReadResult(json.loads(show.stdout), sha.stdout.strip(), None)

    def cas_write(self, expected_sha: str, doc: dict) -> WriteResult:
        try:
            current = self.read()
        except TransportError as e:
            return WriteResult("ambiguous", None, str(e)[:300])
        if (current.sha or "") != (expected_sha or ""):
            return WriteResult("rejected", current.sha, "remote moved")
        self._checkout_expected(current.sha)
        path = self.work / OWNERSHIP_FILE
        path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        add = self._git(["add", "--", OWNERSHIP_FILE])
        if add.returncode != 0:
            raise TransportError((add.stderr or "")[-400:] or "git add failed")
        commit = self._git([
            "-c", "user.email=orb-v4-paper@localhost",
            "-c", "user.name=orb-v4-paper",
            "commit", "-m", f"orb-v4 ownership epoch {doc.get('epoch')}",
        ])
        if commit.returncode != 0:
            raise TransportError((commit.stderr or commit.stdout or "")[-400:] or "git commit failed")
        if not expected_sha:
            push = self._git(["push", "origin", f"HEAD:refs/heads/{self.branch}"])
        else:
            push = self._git([
                "push",
                f"--force-with-lease=refs/heads/{self.branch}:{expected_sha}",
                "origin",
                f"HEAD:refs/heads/{self.branch}",
            ])
        err = (push.stderr or "") + (push.stdout or "")
        if push.returncode != 0:
            if any(s in err.lower() for s in ("rejected", "stale info", "non-fast-forward", "failed to push")):
                return WriteResult("rejected", None, err[-300:])
            return WriteResult("ambiguous", None, err[-300:] or "git push failed")
        new = self._git(["rev-parse", "HEAD"])
        return WriteResult("ok", new.stdout.strip() if new.returncode == 0 else None, "")

    def _ensure(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        if (self.work / ".git").exists():
            return
        init = self._git(["init", "-b", self.branch])
        if init.returncode != 0:
            raise TransportError((init.stderr or "")[-400:] or "git init failed")
        remote = self._git(["remote", "add", "origin", self.remote])
        if remote.returncode != 0:
            raise TransportError((remote.stderr or "")[-400:] or "git remote add failed")

    def _checkout_expected(self, sha: str) -> None:
        if not sha:
            head = self._git(["rev-parse", "--verify", "HEAD"])
            if head.returncode != 0:
                return
            orphan = self._git(["checkout", "--orphan", f"{self.branch}-new"])
            if orphan.returncode != 0:
                raise TransportError((orphan.stderr or "")[-400:] or "orphan checkout failed")
            return
        co = self._git(["checkout", "-B", self.branch, sha])
        if co.returncode != 0:
            raise TransportError((co.stderr or "")[-400:] or "git checkout failed")

    def _git(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=str(self.work),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            raise TransportError("git timeout") from e


def _missing_ref(err: str) -> bool:
    low = err.lower()
    return (
        "couldn't find remote ref" in low
        or "could not find remote ref" in low
        or "remote ref does not exist" in low
    )


_FACTORY = None


def set_transport_factory(fn) -> None:
    global _FACTORY
    _FACTORY = fn


def make_transport_from_env():
    if _FACTORY is not None:
        return _FACTORY()
    mode = (os.environ.get("ORB_V4_OWNERSHIP_TRANSPORT") or "git").strip().lower()
    if mode == "file":
        if os.environ.get("ORB_V4_OWNERSHIP_FILE_IS_TEST") != "1":
            raise OwnershipPrerequisite("ORB_V4_OWNERSHIP_TRANSPORT=file is not a cross-host lock")
        path = os.environ.get("ORB_V4_OWNERSHIP_FILE") or ""
        if not path:
            raise OwnershipPrerequisite("test file transport path unset")
        return _TestFileTransport(Path(path))
    if mode != "git":
        raise OwnershipPrerequisite(f"unsupported ownership transport {mode!r}")
    remote = (os.environ.get("ORB_V4_OWNERSHIP_REMOTE") or "").strip()
    if not remote:
        raise OwnershipPrerequisite("ORB_V4_OWNERSHIP_REMOTE is unset")
    work = Path(os.environ.get("ORB_V4_OWNERSHIP_WORKDIR") or (Path.home() / ".orb-v4-paper" / "run" / "ownership-git"))
    branch = (os.environ.get("ORB_V4_OWNERSHIP_BRANCH") or OWNERSHIP_BRANCH).strip()
    return GitBranchTransport(remote, work, branch=branch)


class _TestFileTransport:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> ReadResult:
        if not self.path.exists():
            return ReadResult(None, "", None)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        return ReadResult(doc.get("doc"), str(doc.get("sha") or ""), None)

    def cas_write(self, expected_sha: str, doc: dict) -> WriteResult:
        cur = self.read()
        if (cur.sha or "") != (expected_sha or ""):
            return WriteResult("rejected", cur.sha, "mismatch")
        sha = str(int(cur.sha or "0") + 1)
        self.path.write_text(json.dumps({"sha": sha, "doc": doc}), encoding="utf-8")
        return WriteResult("ok", sha, "")
