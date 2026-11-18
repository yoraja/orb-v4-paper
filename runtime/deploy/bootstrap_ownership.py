#!/usr/bin/env python3
"""Create the PAPER ownership branch once, using the existing git credential.

Idempotent: if origin already has ownership.json on the branch, this exits
without writing. It does not overwrite a holding lease, does not put a token
on the command line, and does not push the dashboard branch.

    python runtime/deploy/bootstrap_ownership.py --remote https://github.com/yoraja/orb-v4-paper.git
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

_RUNTIME = Path(__file__).resolve().parents[1]
if str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))

from paper_broker.failover.transport import GitBranchTransport, OwnershipPrerequisite, TransportError

INITIAL = {
    "schema": 1,
    "track": "v4paper",
    "account_pin_last4": "J7D0",
    "primary_host": "yopc",
    "owner": None,
    "epoch": 0,
    "token": None,
    "instance": None,
    "status": "released",
    "date": None,
    "slot": None,
    "kind": None,
    "acquired_utc": None,
    "renewed_utc": None,
    "expires_utc": None,
    "reason": "bootstrap",
    "completed": [],
}


def bootstrap_ownership(remote: str, work: Path, branch: str = "ownership") -> str:
    """Return 'created' or 'exists'. Never replaces an existing ownership.json."""
    transport = GitBranchTransport(remote, work, branch=branch)
    current = transport.read()
    if current.doc is not None:
        return "exists"
    result = transport.cas_write(current.sha or "", INITIAL)
    if result.status == "rejected":
        again = transport.read()
        if again.doc is not None:
            return "exists"
        raise SystemExit(f"FAIL CLOSED: ownership bootstrap rejected ({result.detail})")
    if result.status != "ok":
        raise SystemExit(f"FAIL CLOSED: ownership bootstrap {result.status} ({result.detail})")
    seen = transport.read()
    if not seen.doc or seen.doc.get("reason") != "bootstrap" or seen.doc.get("status") != "released":
        raise SystemExit("FAIL CLOSED: ownership bootstrap was not confirmed")
    return "created"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create the PAPER ownership branch if it is absent")
    ap.add_argument("--remote", default="", help="Existing git remote. Defaults to ORB_V4_OWNERSHIP_REMOTE.")
    ap.add_argument("--work", default="", help="Scratch clone. A private temp dir is used when omitted.")
    ap.add_argument("--branch", default="ownership")
    check = sys.argv[1:] if argv is None else argv
    for flag in check:
        if flag in ("--live", "--arm"):
            print(f"FAIL CLOSED: refusing {flag}")
            return 2
    args = ap.parse_args(argv)
    import os
    remote = (args.remote or os.environ.get("ORB_V4_OWNERSHIP_REMOTE") or "").strip()
    if not remote:
        remote = "https://github.com/yoraja/orb-v4-paper.git"
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="orb-v4-ownership-"))
    try:
        action = bootstrap_ownership(remote, work, args.branch)
    except OwnershipPrerequisite as e:
        print(f"FAIL CLOSED: {e}")
        return 2
    except TransportError as e:
        print(f"FAIL CLOSED: {e}")
        return 2
    print(f"ownership branch {args.branch}: {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
