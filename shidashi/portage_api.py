"""Integração protegida (*guarded*) com o Portage do host Gentoo.

Este módulo é a *única* porta de entrada do Shidashi para o módulo ``portage``
(fornecido pelo sistema via ``sys-apps/portage``, não por ``pip``). O import é
protegido: num host não-Gentoo (CI, laptop de desenvolvimento) o módulo carrega
sem levantar exceção e expõe ``PORTAGE_AVAILABLE = False`` (R7.1).

A exceção :class:`PortageUnavailableError` é definida **aqui** — nunca em
:mod:`shidashi.recipe` — para que a camada de receitas permaneça livre de qualquer
acoplamento com Portage (design §2, §9). O motor de receitas e os subcomandos
``recipe`` da CLI não importam este módulo nem chamam :func:`require_portage`.

Os auxiliares de leitura (``portage_version``, ``configured_repos``) chamam
:func:`require_portage` antes de tocar no módulo ``portage``, garantindo uma
mensagem acionável quando ele está indisponível (R7.2).
"""

from types import ModuleType

try:
    # ``portage`` é fornecido pelo sistema e não possui type stubs; o ignore é
    # marcado também como ``unused-ignore`` para permanecer válido nos dois
    # cenários (host Gentoo, onde o import resolve, e host sem Portage).
    import portage as _portage_mod  # type: ignore[import-not-found, unused-ignore]

    _portage: ModuleType | None = _portage_mod
    PORTAGE_AVAILABLE: bool = True
except ImportError:  # host não-Gentoo (CI, laptop de desenvolvimento)
    _portage = None
    PORTAGE_AVAILABLE = False


class PortageUnavailableError(Exception):
    """O Portage não está disponível neste host (R7.2).

    Levantada por :func:`require_portage` (e, por consequência, por qualquer
    auxiliar que dependa dele) quando ``import portage`` falhou no carregamento
    do módulo — tipicamente num host não-Gentoo. A mensagem é acionável,
    indicando o requisito de um host Gentoo com ``sys-apps/portage``.
    """


def require_portage() -> ModuleType:
    """Devolve o módulo ``portage`` ou levanta :class:`PortageUnavailableError`.

    É o *portão* (gate) único pelo qual o restante do Shidashi acessa o Portage. Em
    host não-Gentoo (``PORTAGE_AVAILABLE is False``) levanta
    :class:`PortageUnavailableError` com mensagem acionável (R7.2); caso
    contrário devolve o módulo ``portage`` já carregado.
    """
    if not PORTAGE_AVAILABLE or _portage is None:
        raise PortageUnavailableError(
            "This operation requires a Gentoo host with sys-apps/portage."
        )
    return _portage


def portage_version() -> str:
    """Devolve a versão do Portage como ``str`` (R7.2).

    Chama :func:`require_portage` primeiro (logo, levanta
    :class:`PortageUnavailableError` em host não-Gentoo). Lê o atributo
    ``VERSION`` do módulo ``portage`` de forma defensiva — esse acesso real só
    ocorre num host Gentoo — e devolve sempre uma ``str``.
    """
    portage = require_portage()
    version = getattr(portage, "VERSION", "unknown")
    return str(version)


def configured_repos() -> tuple[str, ...]:
    """Devolve os nomes dos repositórios configurados como tupla (R7.2).

    Chama :func:`require_portage` primeiro (logo, levanta
    :class:`PortageUnavailableError` em host não-Gentoo). Lê a configuração
    global (``portage.settings``) e dela a coleção de repositórios; mantém-se
    defensivo quanto à forma exata da API — esse caminho só roda num host
    Gentoo — devolvendo uma tupla ordenada e estável de nomes (``str``).
    """
    portage = require_portage()
    settings = getattr(portage, "settings", None)
    repositories = getattr(settings, "repositories", None)
    if repositories is None:
        return ()
    prepos = getattr(repositories, "prepos", repositories)
    return tuple(sorted(str(name) for name in prepos))
