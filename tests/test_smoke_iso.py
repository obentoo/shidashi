"""Testes do smoke-test de boot da ISO (OVERVIEW §7, Fase 1).

A montagem real da ISO (squashfs + dracut ``dmsquash-live`` + grub-mkrescue) e o
boot em QEMU são host-gated — exigem root + grub-mkrescue + dracut + qemu + cc — e
ficam em ``scripts/smoke-iso.sh`` (o runbook automatizado). Aqui, no idioma de
tests/test_image.py: checagens off-host de que o runbook existe, é executável e
seu ``--help`` cita os tokens-chave (rodam na suíte CI não-Gentoo), e um teste
host-gated (``@pytest.mark.skipif``) que invoca o boot mínimo self-contained e
assere o sentinela impresso pelo ``/sbin/init`` após o pivot do dmsquash-live.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "smoke-iso.sh"

# Host-gating: o boot real precisa de root + a cadeia de ferramentas de imagem +
# QEMU. Em CI não-Gentoo / sandbox não-root este teste pula (igual aos demais
# host-gated de container/factory).
_NEEDS_HOST = (
    os.geteuid() != 0
    or shutil.which("grub-mkrescue") is None
    or shutil.which("dracut") is None
    or shutil.which("qemu-system-x86_64") is None
    or shutil.which("cc") is None
)
_skip_privileged = pytest.mark.skipif(
    _NEEDS_HOST,
    reason="exige root + grub-mkrescue + dracut + qemu + cc (host Gentoo privilegiado)",
)


# --- runbook presente (off-host) ---------------------------------------------


def test_smoke_runbook_exists_and_executable() -> None:
    assert _SCRIPT.is_file(), "scripts/smoke-iso.sh deve existir (runbook do smoke-test)"
    assert os.access(_SCRIPT, os.X_OK), "scripts/smoke-iso.sh deve ser executável"


def test_smoke_runbook_help_documents_modes() -> None:
    proc = subprocess.run([str(_SCRIPT), "--help"], capture_output=True, text=True, check=True)
    text = proc.stdout + proc.stderr
    # Os dois modos (mínimo/--iso), o módulo de boot e o sentinela documentados.
    for token in ("--iso", "dmsquash-live", "SHIDASHI_SMOKE_OK", "QEMU"):
        assert token in text, f"--help deveria citar {token!r}"


def test_smoke_runbook_rejects_unknown_option() -> None:
    proc = subprocess.run([str(_SCRIPT), "--nope"], capture_output=True, text=True)
    assert proc.returncode == 2  # opção desconhecida → exit 2 (uso)


# --- boot smoke (host-gated) -------------------------------------------------


@_skip_privileged
def test_minimal_iso_boots_in_qemu(tmp_path: Path) -> None:
    # Boot mínimo self-contained: o script monta a ISO via shidashi.image (as
    # funções reais sob teste) e assere o sentinela que o /sbin/init estático
    # imprime na serial após o dmsquash-live pivotar para o squashfs. --work=
    # tmp_path → o pytest é dono da limpeza dos artefatos.
    proc = subprocess.run(
        [str(_SCRIPT), "--work", str(tmp_path), "--timeout", "240"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"smoke-iso falhou:\n{proc.stdout}\n{proc.stderr}"
