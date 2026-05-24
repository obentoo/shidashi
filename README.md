# Kaji 鍛冶

> A forja do **bentoo** — automatiza builds e ISOs de instalação a partir de um stage3 do Gentoo.

Kaji parte sempre de um **stage3 oficial** e aplica a camada bentoo (config + pacotes),
compilando em ambientes isolados por *flavor*, servindo binpkgs em variações de USE via
binhost, e montando ISOs live — em múltiplas arquiteturas e flavors, com lançamentos
semanais (**todo domingo às 00:00**).

📄 Arquitetura completa: **[OVERVIEW.md](OVERVIEW.md)**.

## Estado

**Pré-implementação — Fase 0.** Este repositório contém **apenas a documentação de
arquitetura** (`OVERVIEW.md`). O pacote Python `kaji/`, as receitas (`variants/`) e os
subsistemas (Factory/Assembler) **ainda não existem** — ver roadmap no OVERVIEW.md §17.

Os comandos abaixo descrevem o **alvo** da Fase 0, não o que já roda.

## Requisitos

- **Host Gentoo** (o Kaji usa a API Python do Portage: `import portage`).
- **Python ≥ 3.14**.
- Distribuição planejada: **ebuild** `app-misc/kaji` no overlay bentoo.

## Instalação (dev)

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

## Uso (alvo da Fase 0)

```sh
kaji recipe show v3 minimal systemd   # mostra a receita resolvida (deep-merge dos eixos)
kaji recipe validate v3 kde systemd   # valida o merge dos fragmentos
kaji factory v3 kde systemd           # compila binpkgs → binhost
kaji assemble v3 kde systemd          # monta a ISO do binhost
kaji release --all                    # orquestra a matriz inteira
```

> Piloto inicial: `v3 × minimal × systemd`, depois `v3 × kde × systemd`.

## Modelo

- **Eixos componíveis:** `arch × flavor × init` (ver `variants/`, co-localizado por eixo).
- **Flavors:** `minimal` (só TTY) · `kde` (Qt) · `gnome`/`xfce` (GTK) · `wm` (Wayland-only: Hyprland/Sway/niri).
- **Archs:** `v3` (baseline) · `znver5` (Zen 5, Tier 1) · `arrowlake` (Tier 2, build-only).
- **Dois subsistemas:** Package Factory (compila) + ISO Assembler (monta).
- **Build:** tronco persistente (delta semanal) + wipe total limpo em *toolchain-bump*.
- **Determinismo de entrada:** pin de snapshot do `::gentoo` por release.
- **Linguagem:** Python ≥ 3.14 — porque o Portage *é* uma biblioteca Python.

## Licença

GPL-2.0-or-later.
