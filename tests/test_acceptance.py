"""The organisers' own checker, unmodified, against the running portal."""

import subprocess
import sys

from conftest import ROOT


def test_run_py(server, tmp_path):
    cfg = tmp_path / ".dogfood.toml"
    cfg.write_text((ROOT / ".dogfood.toml").read_text().replace("http://localhost:8080", server))
    out = subprocess.run([sys.executable, str(ROOT / "run.py"), str(cfg), "--fixtures", str(ROOT / "fixtures.json")],
                         capture_output=True, text=True, timeout=60).stdout
    assert "FAIL" not in out and "verified T1 T2" in out, out
