"""PAPER dependency runtime must not follow the box .venv symlink.

On the box, ``.venv`` is a symlink into the shared trading-strategy environment.
Pip-installing through that link would change LIVE/shared packages. PAPER uses
a real ``venv-paper`` directory inside the PAPER home.
"""
from __future__ import annotations

import os
from pathlib import Path


class SharedVenvRefused(RuntimeError):
    pass


def inspect_venv(home: Path) -> dict:
    home = Path(home)
    link = home / ".venv"
    info = {
        "home": str(home),
        "paper_venv": str(home / "venv-paper"),
        "symlink": False,
        "target": None,
        "usable_for_pip": False,
    }
    if link.is_symlink():
        info["symlink"] = True
        info["target"] = os.readlink(link)
        info["reason"] = (
            "Refusing the .venv symlink. Create venv-paper inside the PAPER home "
            "and set ORB_V4_PY to that interpreter. Do not pip-install through .venv "
            "and do not modify the shared trading-strategy environment."
        )
        return info
    info["usable_for_pip"] = True
    info["reason"] = "no .venv symlink; pip target is venv-paper only"
    return info


def assert_paper_python(python_path: Path, home: Path) -> Path:
    home = Path(home)
    python_path = Path(python_path)
    if python_path.is_symlink():
        raise SharedVenvRefused(f"{python_path} is a symlink ({os.readlink(python_path)}); refusing")
    resolved = python_path.resolve(strict=False)
    home_r = home.resolve(strict=False)
    if "trading-strategy" in str(resolved).replace("\\", "/"):
        raise SharedVenvRefused(f"refusing shared trading-strategy interpreter {resolved}")
    paper = (home_r / "venv-paper").resolve(strict=False)
    if resolved != paper and paper not in resolved.parents:
        raise SharedVenvRefused(f"{resolved} is outside {paper}. PAPER pip installs only into venv-paper.")
    return resolved
