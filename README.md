# Shidashi 仕出し

> O catering do **bentoo** — prepara e serve builds e ISOs de instalação a partir de um stage3 do Gentoo.

Shidashi parte sempre de um **stage3 oficial** e aplica a camada bentoo (config + pacotes),
compilando em ambientes isolados por *flavor*, servindo binpkgs em variações de USE via
binhost, e montando ISOs live — em múltiplas arquiteturas e flavors, com lançamentos
semanais (**todo domingo às 00:00**).

📄 Arquitetura completa: **[OVERVIEW.md](OVERVIEW.md)**.

## Estado

**Fases 0 e 1 concluídas (código).** O pacote Python `shidashi/` (14 módulos), as receitas
(`variants/`) e ambos os subsistemas (Package Factory + ISO Assembler) **estão implementados e
cobertos por testes** (381 passando). O boot da ISO live foi validado em QEMU/KVM; a validação de
**build em host Gentoo root** é *host-gated* e segue diferida por design (pilots em andamento).
Fases 2–5 em aberto — ver roadmap no OVERVIEW.md §17 e o cronograma em `.epic/docs/ROADMAP.md`.

Os comandos abaixo já rodam off-host (`recipe`, `pretend`); `factory`/`assemble` exigem host root.

## Requisitos

- **Host Gentoo** (o Shidashi usa a API Python do Portage: `import portage`).
- **Python ≥ 3.14**.
- Distribuição planejada: **ebuild** `app-misc/shidashi` no overlay bentoo.

## Instalação (dev)

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

## Uso

```sh
shidashi recipe show v3 minimal systemd   # mostra a receita resolvida (deep-merge dos eixos)
shidashi recipe validate v3 kde systemd   # valida o merge dos fragmentos
shidashi factory v3 kde systemd           # compila binpkgs → binhost
shidashi assemble v3 kde systemd          # monta a ISO do binhost
shidashi release --all                    # orquestra a matriz inteira
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
