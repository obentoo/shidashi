"""Aquisição verificada do stage3 do Kaji (OVERVIEW §10/§11).

Separa a lógica **pura** (parse do pointer pinado, montagem da URL do mirror,
verificação de digest) da execução **privilegiada** (download, verificação GPG
por shell-out a ``gpg``, extração preservando ownership). A lógica pura é
unit-testada em CI não-Gentoo; o download/extração são exercidos pelos testes
de integração host-gated.

Reprodutibilidade: o stage3 é pinado por ``seeds/stage3.toml`` (filename +
sha256 por init) e verificado por SHA-256 **e** assinatura GPG do ``.DIGESTS.asc``
antes de qualquer extração. Usa apenas stdlib (``tomllib``/``urllib``/``hashlib``/
``tarfile``) mais o ``gpg`` do host.
"""

import hashlib
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_STRICT = ConfigDict(frozen=True, extra="forbid")

# Limite defensivo ao ler o pointer TOML (arquivo pequeno e checado-in).
_MAX_POINTER_BYTES = 64 * 1024


class SeedError(Exception):
    """Falha ao adquirir/verificar um stage3 (R2.2/R2.4/R2.6).

    Levantada quando o init não tem entrada pinada, quando a verificação de
    digest/assinatura falha, ou quando ``--no-download`` é usado sem cache.
    """


class Stage3Pointer(BaseModel):
    """Entrada pinada de um stage3 por init (R2.1).

    Frozen pydantic v2 (``extra="forbid"``, idioma de ``recipe.py``). Carrega o
    ``init`` resolvido, a ``base_url`` do mirror, o ``snapshot`` (diretório de
    autobuild), o ``filename`` do tarball e seu ``sha256`` pinado.
    """

    model_config = _STRICT
    init: str
    base_url: str
    snapshot: str
    filename: str
    sha256: str


def load_pointer(init: str, *, seeds_dir: Path) -> Stage3Pointer:
    """Lê a entrada pinada de ``init`` em ``seeds/stage3.toml`` (R2.1/R2.2).

    ``snapshot`` e ``base_url`` são chaves de topo compartilhadas; cada init é
    uma tabela com ``filename`` + ``sha256``. Se ``init`` não tiver tabela,
    levanta :class:`SeedError` nomeando o init e as entradas disponíveis.
    """
    toml_path = seeds_dir / "stage3.toml"
    try:
        raw = toml_path.read_bytes()
    except OSError as err:
        raise SeedError(f"não foi possível ler {toml_path}: {err}") from err
    if len(raw) > _MAX_POINTER_BYTES:
        raise SeedError(f"{toml_path} excede o tamanho esperado de pointer")
    data = tomllib.loads(raw.decode("utf-8"))

    snapshot = data.get("snapshot")
    base_url = data.get("base_url")
    if not isinstance(snapshot, str) or not isinstance(base_url, str):
        raise SeedError(f"{toml_path} sem 'snapshot'/'base_url' de topo válidos")

    # tabelas de init = todas as chaves cujo valor é um mapeamento
    inits = sorted(k for k, v in data.items() if isinstance(v, dict))
    entry = data.get(init)
    if not isinstance(entry, dict):
        disponiveis = ", ".join(inits) if inits else "(nenhuma)"
        raise SeedError(f"init {init!r} sem entrada em {toml_path}; disponíveis: {disponiveis}")

    filename = entry.get("filename")
    sha256 = entry.get("sha256")
    if not isinstance(filename, str) or not isinstance(sha256, str):
        raise SeedError(f"entrada {init!r} em {toml_path} sem 'filename'/'sha256'")

    return Stage3Pointer(
        init=init,
        base_url=base_url,
        snapshot=snapshot,
        filename=filename,
        sha256=sha256,
    )


def stage3_url(pointer: Stage3Pointer) -> str:
    """Monta a URL do tarball no mirror (R2.1). Pura, sem barras duplicadas.

    ``<base_url>/<snapshot>/<filename>`` (o layout de autobuilds da Gentoo).
    """
    base = pointer.base_url.rstrip("/")
    return f"{base}/{pointer.snapshot}/{pointer.filename}"


def verify_digest(tarball: Path, sha256: str) -> None:
    """Compara o SHA-256 de ``tarball`` ao digest pinado (R2.3/R2.4). Pura.

    Lê em blocos para não carregar o tarball inteiro em memória. Em divergência
    levanta :class:`SeedError` nomeando o esperado e o obtido.
    """
    h = hashlib.sha256()
    with tarball.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != sha256:
        raise SeedError(
            f"sha256 divergente para {tarball.name}: esperado {sha256}, obtido {actual}"
        )


def verify_signature(tarball: Path, *, digests: Path) -> None:
    """Verifica a assinatura GPG do ``.DIGESTS.asc`` do tarball (R2.3/R2.4).

    Shell-out a ``gpg --verify`` sobre o ``.DIGESTS.asc`` (que carrega os
    hashes do tarball assinados pela chave de release da Gentoo, confiada no
    keyring do host). Levanta :class:`SeedError` se o arquivo de assinatura
    estiver ausente, se o ``gpg`` não estiver disponível, ou se a verificação
    retornar não-zero. Nunca ignora o código de retorno.
    """
    if not digests.is_file():
        raise SeedError(f".DIGESTS.asc ausente: {digests}")
    if shutil.which("gpg") is None:
        raise SeedError("gpg indisponível no host; impossível verificar a assinatura")
    try:
        result = subprocess.run(
            ["gpg", "--verify", str(digests), str(tarball)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as err:
        raise SeedError(f"falha ao executar gpg --verify: {err}") from err
    if result.returncode != 0:
        raise SeedError(f"verificação GPG falhou para {tarball.name}:\n{result.stderr.strip()}")


def _download(url: str, dest: Path) -> None:
    """Baixa ``url`` para ``dest`` via stdlib ``urllib`` (privilegiado/rede).

    Isolado num helper nomeado para que os testes possam monkeypatchar
    ``seed._download`` e garantir que o caminho de cache-hit não toque a rede.
    """
    try:
        with urllib.request.urlopen(url) as resp, dest.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out)
    except (urllib.error.URLError, OSError) as err:
        raise SeedError(f"falha ao baixar {url}: {err}") from err


def fetch_stage3(pointer: Stage3Pointer, *, cache_dir: Path, download: bool = True) -> Path:
    """Devolve o tarball verificado, baixando-o uma vez se preciso (R2.5/R2.6).

    - Se ``cache_dir/<filename>`` já existe e bate o digest pinado, reusa sem
      tocar a rede (R2.5).
    - Caso contrário, se ``download`` for ``False``, levanta :class:`SeedError`
      acionável sem rede (R2.6).
    - Senão baixa tarball **e** sibling ``<filename>.DIGESTS.asc`` para arquivos
      temporários, verifica digest + assinatura (apagando os parciais em falha),
      e só então move atomicamente o tarball para o cache.
    """
    cached = cache_dir / pointer.filename
    if cached.is_file():
        try:
            verify_digest(cached, pointer.sha256)
        except SeedError:
            pass  # cache corrompido/desatualizado → rebaixa abaixo
        else:
            return cached

    if not download:
        raise SeedError(
            f"--no-download: stage3 {pointer.filename!r} ausente do cache {cache_dir} "
            f"e download desabilitado; rode sem --no-download para obtê-lo"
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    url = stage3_url(pointer)
    asc_url = f"{url}.DIGESTS.asc"
    tmp_dir = Path(tempfile.mkdtemp(prefix="kaji-seed-", dir=cache_dir))
    tmp_tarball = tmp_dir / pointer.filename
    tmp_asc = tmp_dir / f"{pointer.filename}.DIGESTS.asc"
    try:
        _download(url, tmp_tarball)
        _download(asc_url, tmp_asc)
        verify_digest(tmp_tarball, pointer.sha256)
        verify_signature(tmp_tarball, digests=tmp_asc)
        tmp_tarball.replace(cached)
    except SeedError:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return cached


def extract_stage3(tarball: Path, rootfs: Path) -> None:
    """Extrai o tarball verificado em ``rootfs`` preservando ownership.

    **Privilegiado** (requer root): preserva donos/permissões/devices do
    stage3. Cria ``rootfs`` sob o scratch. Em erro de extração levanta
    :class:`SeedError`.
    """
    rootfs.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tarball, "r:*") as tar:
            tar.extractall(rootfs, filter="tar")  # filter=tar preserva metadados
    except (tarfile.TarError, OSError) as err:
        raise SeedError(f"falha ao extrair {tarball.name} em {rootfs}: {err}") from err
