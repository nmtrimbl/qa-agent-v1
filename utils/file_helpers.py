from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[1]


def new_run_id() -> str:
    return uuid4().hex


def abs_path(relative_or_abs: str | Path) -> Path:
    p = Path(relative_or_abs)
    if p.is_absolute():
        return p
    return (REPO_ROOT / p).resolve()


def ensure_dir(path: str | Path) -> Path:
    p = abs_path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_filename(name: str) -> str:
    """
    Make a file name safe-ish for most OSes.
    """

    name = name.strip().replace(" ", "_")
    name = re.sub(r"[^a-zA-Z0-9_\-\.]+", "", name)
    return name or "item"


def write_json(path: str | Path, data: Any) -> Path:
    path = abs_path(path)
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_text(path: str | Path, text: str) -> Path:
    path = abs_path(path)
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")
    return path

