import os
import subprocess

import pytest

from jevme.learned import KINDS, LearnedSpec

EVIL = 'hi"; $(touch {p}) `touch {p}` \' & do shell script "touch {p}" & "'


@pytest.mark.parametrize("kind,script", [
    ("shell", 'echo "{{m}}"'), ("shell", "echo '{{m}}'"), ("shell", "echo {{m}}"),
    ("applescript", 'return "{{m}}"'), ("applescript", "return {{m}}"),
    ("jxa", "'{{m}}'"), ("jxa", '"{{m}}"'), ("jxa", "`{{m}}`"), ("jxa", "{{m}}"),
])
def test_spoken_values_never_become_code(kind, script, tmp_path):
    marker = tmp_path / "pwned"
    evil = EVIL.format(p=marker)
    spec = LearnedSpec("t", "t", [], kind, script, "ok", [{"name": "m", "kind": "text"}])
    src, env = spec.render({"m": evil})
    r = subprocess.run(KINDS[kind] + ([src] if kind == "shell" else ["-e", src]),
                       capture_output=True, text=True, env={**os.environ, **env}, timeout=20)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == evil
    assert not marker.exists()
