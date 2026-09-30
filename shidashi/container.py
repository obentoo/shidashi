"""Container — wrapper sobre ``systemd-nspawn`` para builds isolados (OVERVIEW §12).

O isolamento é feito por ``systemd-nspawn`` (OVERVIEW §12): ``emerge``/``eselect``
rodam *dentro* do container, nunca via ``import portage``. A construção da linha
de comando (:func:`_nspawn_argv`) é **pura e inspecionável** — testável sem root;
a execução (:meth:`Container.run`) e o ciclo de vida (context manager) exigem
root + ``systemd-nspawn`` e são exercidos pelos testes de integração host-gated.
"""

import datetime
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import TracebackType

from shidashi import audit


class CommandResult:
    """Resultado de um comando executado dentro do container (OVERVIEW §12).

    Value object: código de saída e os fluxos ``stdout``/``stderr`` capturados.
    """

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


#: The lab's validated nspawn line (start.sh). --resolv-conf=copy-host: fetches
#: must not depend on whatever resolv.conf the stage3 ships. --register=no: not a
#: --boot container, so there is nothing for systemd-machined to manage.
#: --timezone=off: nspawn's default (auto) writes the BUILD HOST's zone into the
#: image -- the first ISO shipped America/Sao_Paulo that way (F79); the image's
#: zone is system.yaml's, set by shidashi.system.
_HOST_OPTIONS = ("--register=no", "--resolv-conf=copy-host", "--timezone=off")

# Commands (not the interactive shell) add --console=pipe and get stdin from
# /dev/null. nspawn's default console is INTERACTIVE when it is started from a
# terminal -- a factory run inside tmux -- which would hand the build the
# user's keyboard and raw tty for hours; otherwise it is read-only, still
# through a pty that merges stderr into stdout. Pipe mode passes our pipes
# straight through, which nspawn(1) documents as safe for pipe descriptors.
#
# They also run with --as-pid2: a stub init is PID 1 and reaps orphans, and the
# command is PID 2 like any process. Run as PID 1 itself, locale-gen aborted
# with "not all of the selected locales were compiled" (its wait() accounting
# broke); as a child it succeeds -- reproduced in a user+pid namespace on
# 2026-09-26. The lab never saw it: its commands ran under a shell.


def _emit_binds(
    binds: Sequence[tuple[Path, Path]],
    binds_rw: Sequence[tuple[Path, Path]],
) -> list[str]:
    """Bloco de binds do ``systemd-nspawn``: ``--bind-ro=`` (RO) e depois ``--bind=`` (RW).

    Emite ``--bind-ro=src:dst`` por bind RO na ordem declarada, seguido de
    ``--bind=src:dst`` por bind RW na ordem declarada (todos os RW *após* os RO).
    Único ponto de verdade do bloco — compartilhado por :func:`_nspawn_argv` e
    :func:`_nspawn_shell_argv` para que a paridade seja estrutural (R7.2).
    """
    block: list[str] = []
    for src, dst in binds:
        block.append(f"--bind-ro={src}:{dst}")
    for src, dst in binds_rw:
        block.append(f"--bind={src}:{dst}")
    return block


def _nspawn_argv(
    rootfs: Path,
    argv: Sequence[str],
    *,
    binds: Sequence[tuple[Path, Path]],
    binds_rw: Sequence[tuple[Path, Path]] = (),
    ephemeral: bool,
) -> list[str]:
    """Monta a linha de comando ``systemd-nspawn`` (R4.4/R7.1/R7.2). **Pura**, sem efeitos.

    Forma: ``["systemd-nspawn", "--directory", <rootfs>, ("--ephemeral")?,
    ("--bind-ro=src:dst" por bind RO, na ordem), ("--bind=src:dst" por bind RW,
    na ordem, *após* todos os RO), "--", *argv]``. Os binds vêm antes do
    separador ``--``; o ``argv`` do comando vem depois. Sem ``binds_rw`` o argv
    é idêntico ao da story 002 (R7.3 back-compat).
    """
    cmd: list[str] = [
        "systemd-nspawn", "--directory", str(rootfs), *_HOST_OPTIONS,
        "--console=pipe", "--as-pid2",
    ]
    if ephemeral:
        cmd.append("--ephemeral")
    cmd.extend(_emit_binds(binds, binds_rw))
    cmd.append("--")
    cmd.extend(argv)
    return cmd


def _nspawn_shell_argv(
    rootfs: Path,
    *,
    binds: Sequence[tuple[Path, Path]] = (),
    binds_rw: Sequence[tuple[Path, Path]] = (),
) -> list[str]:
    """Monta a linha de um shell interativo ``systemd-nspawn`` (R7.1/R7.2). **Pura**.

    Forma: ``["systemd-nspawn", "--directory", <rootfs>, ("--bind-ro=src:dst"),
    ("--bind=src:dst" após os RO)]`` — o MESMO bloco de binds de
    :func:`_nspawn_argv` (via :func:`_emit_binds`), porém **SEM** comando final
    (sem o separador ``--`` nem argv), de modo que o nspawn caia no shell de
    login do container. Não altera o argv de :func:`_nspawn_argv` (R8.4).
    """
    cmd: list[str] = ["systemd-nspawn", "--directory", str(rootfs), *_HOST_OPTIONS]
    cmd.extend(_emit_binds(binds, binds_rw))
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
        binds_rw: Sequence[tuple[Path, Path]] = (),
        log: Path | None = None,
    ) -> None:
        self.rootfs = rootfs
        self.ephemeral = ephemeral
        self.binds: tuple[tuple[Path, Path], ...] = tuple(binds)
        self.binds_rw: tuple[tuple[Path, Path], ...] = tuple(binds_rw)
        #: When set, every command's output is appended here AS IT RUNS. A base
        #: build is hours of emerge; without this nothing is visible until the
        #: end, and a killed run leaves no trace at all.
        self.log = log

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
        cmd = _nspawn_argv(
            self.rootfs,
            argv,
            binds=self.binds,
            binds_rw=self.binds_rw,
            ephemeral=self.ephemeral,
        )
        if self.log is not None:
            return self._run_logged(cmd, argv, env=env, check=check)
        start = time.monotonic()
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=dict(env) if env is not None else None,
            check=False,
        )
        self._audit(argv, proc.returncode, start, (proc.stdout + proc.stderr).count("\n"))
        result = CommandResult(proc.returncode, proc.stdout, proc.stderr)
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(
                proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr
            )
        return result

    def _run_logged(
        self,
        cmd: list[str],
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None,
        check: bool,
    ) -> CommandResult:
        """Run ``cmd`` streaming its merged stdout+stderr into :attr:`log`.

        Returns the same :class:`CommandResult` as the captured path, with the
        merged stream as ``stdout`` (every caller reads ``stdout + stderr``).
        """
        assert self.log is not None
        self.log.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        start = time.monotonic()
        with self.log.open("a", encoding="utf-8") as out:
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            out.write(f"### {stamp} $ {' '.join(argv)}\n")
            out.flush()
            with subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                env=dict(env) if env is not None else None,
            ) as proc:
                assert proc.stdout is not None
                for line in proc.stdout:
                    lines.append(line)
                    out.write(line)
                    out.flush()
                returncode = proc.wait()
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            out.write(f"### {stamp} exit {returncode}\n")
        self._audit(argv, returncode, start, len(lines))
        output = "".join(lines)
        if check and returncode != 0:
            raise subprocess.CalledProcessError(returncode, cmd, output=output, stderr="")
        return CommandResult(returncode, output, "")

    def _audit(self, argv: Sequence[str], exit_code: int, start: float, lines: int) -> None:
        """One ``command`` event in the run's audit trail (a no-op outside a run)."""
        audit.current().command(
            argv,
            exit_code=exit_code,
            duration_s=round(time.monotonic() - start, 3),
            output_lines=lines,
            rootfs=str(self.rootfs),
            log=str(self.log) if self.log is not None else None,
        )

    def shell(self) -> None:
        """Abre um shell interativo no rootfs *vivo* e devolve quando ele sai (R7.1/R7.3).

        Monta a linha via :func:`_nspawn_shell_argv` (mesmos binds RO/RW do
        build) e executa com ``subprocess.run`` de stdio **herdado** — sem
        ``capture_output``/``text``/``check`` — para que o terminal do usuário
        se acople ao container. Reaproveita o rootfs persistente
        (``ephemeral=False``), então mudanças feitas no shell persistem na
        próxima fase; retorna ao sair **sem** derrubar o rootfs.
        """
        subprocess.run(_nspawn_shell_argv(self.rootfs, binds=self.binds, binds_rw=self.binds_rw))

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
