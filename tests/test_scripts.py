"""Testes dos scripts shell do Kaji (R10.1).

Cobertura: ``scripts/pretend-resolve.sh`` é sintaticamente válido (`bash -n`),
declara ``set -euo pipefail`` e carrega os marcadores ``TODO(phase-2)`` que
documentam as etapas dependentes de host Gentoo real.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pretend-resolve.sh"


def test_script_exists() -> None:
    assert _SCRIPT.is_file(), "scripts/pretend-resolve.sh ausente"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash indisponível")
def test_script_passes_bash_syntax_check() -> None:
    result = subprocess.run(
        ["bash", "-n", str(_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"bash -n falhou: {result.stderr}"


def test_script_declares_strict_mode_and_todos() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "TODO(phase-2)" in text, "marcadores de fase 2 ausentes"
    # a forma seed → apply_portage → run --pretend está presente
    for fn in ("seed_stage3", "apply_portage", "run_pretend"):
        assert fn in text, f"etapa {fn!r} ausente no esqueleto"
