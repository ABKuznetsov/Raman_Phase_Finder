from __future__ import annotations

import os
from pathlib import Path

from vibrational_finder.core.app_paths import raman_data_root


def app_cache_dir() -> Path:
    configured = os.environ.get("IR_RAMAN_PHASE_FINDER_CACHE_DIR")
    root = Path(configured) if configured else raman_data_root() / "cache"
    root.mkdir(parents=True, exist_ok=True)
    return root
