"""Throwaway data directory for the test scripts: C4AI_DATA_DIR under
.scratch/, set before `clicker4ai` is imported and removed at exit, so a
failed run leaves no devices, pairing codes or sessions in the live
~/.clicker4ai. Only allowed_roots (~/work) is configured."""

import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def isolate(name: str) -> Path:
    (ROOT / ".scratch").mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix=f"c4ai-{name}-", dir=ROOT / ".scratch"))
    (d / "config.json").write_text(json.dumps(
        {"allowed_roots": [str(Path.home() / "work")]}))
    os.environ["C4AI_DATA_DIR"] = str(d)
    atexit.register(shutil.rmtree, d, True)
    return d
