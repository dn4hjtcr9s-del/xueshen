"""RAG 验收 CLI 测试：确保可从任意目录按文件路径运行。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_rag_verify_script_runs_help_from_any_cwd(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "rag_verify.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--sample-count" in completed.stdout
    assert "--fts-query" in completed.stdout
