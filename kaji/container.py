"""Container — wrapper sobre ``systemd-nspawn`` para builds isolados (OVERVIEW §12).

Esqueleto da Fase 0: apenas as assinaturas públicas tipadas. O isolamento de
build é feito por ``systemd-nspawn`` (OVERVIEW §12) e os comandos ``emerge``/
``eselect`` rodam *dentro* do container — montados de forma segura contra
injeção (t-strings, OVERVIEW §12). Nada aqui executa ainda: cada corpo levanta
``NotImplementedError``.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType


class CommandResult:
    """Resultado de um comando executado dentro do container (OVERVIEW §12).

    Carrega o código de saída e os fluxos capturados. Esqueleto: o construtor
    ainda não é implementado.
    """

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        raise NotImplementedError("Fase 0 — ver OVERVIEW §12")


class Container:
    """Wrapper de um ``systemd-nspawn`` com raiz em ``rootfs`` (OVERVIEW §12).

    Encapsula o ciclo de vida do container (boot/desligamento) e a execução de
    comandos isolados. Esqueleto: nenhum método é implementado.
    """

    def __init__(self, rootfs: Path, *, ephemeral: bool = False) -> None:
        raise NotImplementedError("Fase 0 — ver OVERVIEW §12")

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        """Executa ``argv`` dentro do container e devolve o resultado."""
        raise NotImplementedError("Fase 0 — ver OVERVIEW §12")

    def __enter__(self) -> Container:
        raise NotImplementedError("Fase 0 — ver OVERVIEW §12")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError("Fase 0 — ver OVERVIEW §12")
