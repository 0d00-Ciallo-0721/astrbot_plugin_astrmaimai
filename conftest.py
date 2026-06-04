"""pytest configuration — ensures the project root is on sys.path so that
``import astrmai`` works regardless of the working directory or PYTHONPATH."""

import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
