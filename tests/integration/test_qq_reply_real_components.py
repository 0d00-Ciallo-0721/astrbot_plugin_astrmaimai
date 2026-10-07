import os
from pathlib import Path
import subprocess
import sys


def test_installed_astrbot_components_offline_replay(tmp_path):
    """Use an isolated interpreter to load installed modules, never test stubs."""
    root = Path(__file__).resolve().parents[2]
    script = root / "tests/helpers/qq_reply_real_components_replay.py"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-B", "-c",
         "import runpy,sys; sys.path.insert(0,sys.argv[1]); runpy.run_path(sys.argv[2],run_name='__main__')",
         str(root), str(script)], cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REAL_COMPONENT_REPLAY=" in result.stdout
