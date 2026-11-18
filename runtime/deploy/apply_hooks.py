"""Insert failover hooks into an already-installed PAPER engine.

The installer refuses to write when the expected anchor is missing, and it
refuses to touch the dashboard root, a recovery-task file, or a symlinked venv.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

DASHBOARD_FILES = {"index.html", "strategy.html", "heartbeat.json", ".nojekyll"}
RECOVERY_SHA256 = {
    "orb-v4-recovery-close-20261109.py": "58a8ff246eb12f561f49ca59a91a89f5c518518717c424ca22f17cf5898d1fa6",
    "orb-v4-recovery-close-20261109.ps1": "306e7ad8ff2eadc921df72c016aefba70016ef252e077b3c5f5e5d5db20e6c3d",
    "task-ORB-v4-PAPER-Recovery-Close-20261109.xml": "d8d3d276772a17e495fbb5ac66b334537cfc5be9b28049d6f6c5ea337a298a84",
}

EXIT_ANCHOR = (
    'for r in sorted(due, key=lambda x: x["symbol"]):\n'
    '        if r["symbol"] in have_moc:'
)
EXIT_REPLACEMENT = (
    'for r in sorted(due, key=lambda x: x["symbol"]):\n'
    '        _pos = {p["symbol"]: p for p in eng.list_positions(client)}.get(r["symbol"])\n'
    '        from paper_broker.failover.close_guard import decide_close as _decide_close\n'
    '        _d = _decide_close(r, _pos, oo, today=today)\n'
    '        if not _d.get("submit"):\n'
    '            out.append({"symbol": r["symbol"], "action": _d["action"], "guard": _d})\n'
    '            continue\n'
    '        r = dict(r)\n'
    '        r["qty"] = _d["qty"]\n'
    '        if r["symbol"] in have_moc:'
)
FLATTEN_ANCHOR = "    if flat_syms:\n        fo = flatten(client, sorted(flat_syms), dry_run=dry_run, log=log)"
FLATTEN_REPLACEMENT = (
    "    if flat_syms:\n"
    "        from paper_broker.failover.close_guard import decide_close as _decide_close\n"
    "        _kept = {}\n"
    "        for _sym, _why in list(flat_syms.items()):\n"
    "            _p = pos.get(_sym)\n"
    "            _rec = cb['positions'].get(_sym) or {'symbol': _sym, 'side': None, 'qty': (_p or {}).get('qty')}\n"
    "            _d = _decide_close(_rec, _p, oo, today=today)\n"
    "            if not _d.get('submit'):\n"
    "                acts.append({'symbol': _sym, 'action': _d['action'], 'guard': _d, 'skipped_flatten': _why})\n"
    "            else:\n"
    "                _kept[_sym] = _why\n"
    "        flat_syms = _kept\n"
    "    if flat_syms:\n"
    "        fo = flatten(client, sorted(flat_syms), dry_run=dry_run, log=log)"
)
# Installed paper_broker/orb_v4_paper.py on both hosts (sha256
# 97c073d4b5a85d6cbcfeeae53a0c283f3b23b803a9cf4d11f4e66b5c04f23fe7)
# assigns the broker result to `o`. Downstream code reads `o`, so the hook
# must not rename it. `submitted = client.submit_order(req)` is not this file.
SUBMIT_LINE = "o = client.submit_order(req)"


class HookApplyError(RuntimeError):
    pass


def assert_not_dashboard(dest: Path) -> None:
    dest = Path(dest)
    present = {name for name in DASHBOARD_FILES if (dest / name).exists()}
    if present and not (dest / "paper_broker").is_dir():
        raise HookApplyError(
            f"refusing to write into dashboard root {dest} (found {sorted(present)})"
        )


def recovery_status(path: Path) -> str:
    """match, missing, or mismatch. Never rewrites the file."""
    path = Path(path)
    expected = RECOVERY_SHA256.get(path.name)
    if expected is None:
        raise HookApplyError(f"{path.name} is not a pinned recovery file")
    if not path.is_file():
        return "missing"
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != expected:
        return "mismatch"
    return "match"


def _submit_lines_guarded(source: str) -> bool:
    lines = source.splitlines()
    found = False
    for index, line in enumerate(lines):
        if line.strip() != SUBMIT_LINE:
            continue
        found = True
        previous = [lines[pos].strip() for pos in range(max(0, index - 3), index)]
        if "before_submit()" not in previous:
            return False
    return found


def _insert_before_submit(source: str) -> str:
    """Insert before_submit() above each `o = client.submit_order(req)` and keep `o`."""
    lines = source.splitlines(keepends=True)
    found = 0
    out: list[str] = []
    for line in lines:
        if line.strip() != SUBMIT_LINE:
            out.append(line)
            continue
        indent = line[:len(line) - len(line.lstrip(" "))]
        previous = [item.strip() for item in out[-3:]]
        if "before_submit()" not in previous:
            out.append(f"{indent}from paper_broker.failover.fence import before_submit\n")
            out.append(f"{indent}before_submit()\n")
        if not line.endswith("\n"):
            line += "\n"
        out.append(line)
        found += 1
    if found == 0:
        raise HookApplyError(
            "installed PAPER engine does not match the known anchors (submit_order). "
            "Refusing to patch. Leave the current roles in place."
        )
    return "".join(out)


def apply_engine_hooks(source: str) -> str:
    if "decide_close" in source and _submit_lines_guarded(source):
        return source
    missing = []
    if EXIT_ANCHOR not in source:
        missing.append("cmd_exit loop")
    if FLATTEN_ANCHOR not in source:
        missing.append("cmd_eod flatten")
    if SUBMIT_LINE not in source:
        missing.append("submit_order")
    if missing:
        raise HookApplyError(
            "installed PAPER engine does not match the known anchors ("
            + ", ".join(missing)
            + "). Refusing to patch. Leave the current roles in place."
        )
    out = source.replace(EXIT_ANCHOR, EXIT_REPLACEMENT, 1)
    out = out.replace(FLATTEN_ANCHOR, FLATTEN_REPLACEMENT, 1)
    return _insert_before_submit(out)


TIMEOUT_MARKER = "install_runtime_timeouts()"
TIMEOUT_BLOCK = (
    "try:\n"
    "    from paper_broker.failover.http_timeouts import install_runtime_timeouts\n"
    "    install_runtime_timeouts()\n"
    "except Exception:\n"
    "    pass\n"
)
TIMEOUT_ANCHOR = "import paper_broker.run_orb_v3_paper as eng"


def apply_timeout_install(source: str) -> str:
    """Idempotent: ensure the installed engine installs HTTP timeouts at import.

    Does not change entry, exit, sizing, or risk logic.
    """
    if TIMEOUT_MARKER in source:
        return source
    if TIMEOUT_ANCHOR not in source:
        raise HookApplyError(
            "installed PAPER engine does not match the known anchors "
            "(run_orb_v3_paper import). Refusing to patch. Leave the current roles in place."
        )
    return source.replace(TIMEOUT_ANCHOR, TIMEOUT_ANCHOR + "\n" + TIMEOUT_BLOCK, 1)
