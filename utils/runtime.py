from __future__ import annotations

import sys
from pathlib import Path

from utils.file_helpers import ensure_dir


def ensure_project_root_on_path(project_root: str | Path) -> Path:
    """
    Add the repository root to `sys.path` if it is missing.

    This keeps imports working in entrypoints like Streamlit and pytest.
    """

    project_root = Path(project_root).resolve()
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)
    return project_root


def ensure_runtime_directories(*, artifacts_dir: str | Path, logs_dir: str | Path) -> tuple[Path, Path]:
    """
    Create the main runtime directories used by the app.
    """

    return ensure_dir(artifacts_dir), ensure_dir(logs_dir)
