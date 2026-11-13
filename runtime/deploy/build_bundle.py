"""Build the PAPER failover overlay archive. Excludes dashboard, git, and secrets."""
from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
EXCLUDE_PARTS = {
    ".git", "__pycache__", ".venv", "venv-paper", "index.html", "strategy.html",
    "heartbeat.json", ".nojekyll", ".env",
}


def _allowed(path: Path) -> bool:
    rel = path.relative_to(RUNTIME)
    if any(part in EXCLUDE_PARTS for part in rel.parts):
        return False
    name = path.name.lower()
    if "secret" in name or name.endswith(".pem") or name.endswith(".key"):
        return False
    return path.is_file()


def build(dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for path in sorted(RUNTIME.rglob("*")):
            if not _allowed(path):
                continue
            tar.add(path, arcname=str(Path("runtime") / path.relative_to(RUNTIME)))
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    dest.with_suffix(dest.suffix + ".sha256").write_text(digest + "  " + dest.name + "\n", encoding="utf-8")
    return digest


if __name__ == "__main__":
    out = ROOT / "dist" / "orb-v4-paper-yopc-primary.tar.gz"
    print(build(out))
