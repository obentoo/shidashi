"""Container — wrapper sobre ``systemd-nspawn`` para builds isolados (OVERVIEW §12).

O isolamento é feito por ``systemd-nspawn`` (OVERVIEW §12): ``emerge``/``eselect``
rodam *dentro* do container, nunca via ``import portage``. A construção da linha
de comando (:func:`_nspawn_argv`) é **pura e inspecionável** — testável sem root;
a execução (:meth:`Container.run`) e o ciclo de vida (context manager) exigem
root + ``systemd-nspawn`` e são exercidos pelos testes de integração host-gated.
"""

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType


class CommandResult:
    """Resultado de um comando executado dentro do container (OVERVIEW §12).

    Value object: código de saída e os fluxos ``stdout``/``stderr`` capturados.
    """

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _nspawn_argv(
    rootfs: Path,
    argv: Sequence[str],
    *,
    binds: Sequence[tuple[Path, Path]],
    ephemeral: bool,
) -> list[str]:
    """Monta a linha de comando ``systemd-nspawn`` (R4.4). **Pura**, sem efeitos.

    Forma: ``["systemd-nspawn", "--directory", <rootfs>, ("--ephemeral")?,
    ("--bind-ro=src:dst" por bind, na ordem), "--", *argv]``. Os binds vêm antes
    do separador ``--``; o ``argv`` do comando vem depois.
    """
    cmd: list[str] = ["systemd-nspawn", "--directory", str(rootfs)]
    if ephemeral:
        cmd.append("--ephemeral")
    for src, dst in binds:
        cmd.append(f"--bind-ro={src}:{dst}")
    cmd.append("--")
    cmd.extend(argv)
    return cmd


class Container:
    """Wrapper de um ``systemd-nspawn`` com raiz em ``rootfs`` (OVERVIEW §12).

    Context manager: ``__enter__`` valida o ambiente (root + ``systemd-nspawn``
    presentes) e ``__exit__`` faz o teardown do scratch efêmero. Quando
    ``ephemeral=True``, o ``--ephemeral`` do nspawn já deixa o rootfs seedado
    imutável (overlay descartável); o teardown remove o diretório de scratch
    apenas quando ele *não* é efêmero não-preservado.
    """

    def __init__(
        self,
        rootfs: Path,
        *,
        ephemeral: bool = False,
        binds: Sequence[tuple[Path, Path]] = (),
    ) -> None:
        self.rootfs = rootfs
        self.ephemeral = ephemeral
        self.binds: tuple[tuple[Path, Path], ...] = tuple(binds)

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> CommandResult:
        """Executa ``argv`` dentro do container e devolve o resultado (R4.1/R4.3).

        Constrói a linha via :func:`_nspawn_argv` e executa com
        ``subprocess.run`` capturando os fluxos. Quando ``check`` é ``True`` e o
        comando sai com código não-zero, levanta ``CalledProcessError`` (com o
        ``stderr`` anexado), em vez de retornar silenciosamente.
        """
        cmd = _nspawn_argv(self.rootfs, argv, binds=self.binds, ephemeral=self.ephemeral)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=dict(env) if env is not None else None,
            check=False,
        )
        result = CommandResult(proc.returncode, proc.stdout, proc.stderr)
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
            )
        return result

    def __enter__(self) -> Container:
        if shutil.which("systemd-nspawn") is None:
            raise RuntimeError("systemd-nspawn ausente no host")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Teardown do scratch: só removemos quando o container é efêmero (rootfs
        # descartável). ignore_errors=True garante que um teardown falho não
        # mascare uma exceção em voo.
        if self.ephemeral and self.rootfs.exists():
            shutil.rmtree(self.rootfs, ignore_errors=True)
