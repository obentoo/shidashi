# Shidashi 仕出し — Proposta de Desenvolvimento

> Documento de visão e arquitetura do **Shidashi**, a ferramenta de automação de builds e ISOs do **bentoo**.
> Status: **Fase 0 concluída** (scaffold + recipe + `pretend` + `factory`, validados off-host; pilots em host root diferidos) · **Fase 1 (ISO) em andamento** · Linguagem: **Python ≥ 3.14** · Última atualização: 2026-06-04

---

## 1. Sumário Executivo

**bentoo** é uma distribuição derivada do Gentoo — formalmente um *stage4*, apelidada de *"stage5"* — construída **por cima de um stage3 oficial**, com configurações curadas e um conjunto adicional de pacotes.

O **Shidashi** (`仕出し`, "catering — produz lotes sob encomenda e entrega") é a ferramenta que automatiza todo o ciclo: detecta o stage3 mais recente, aplica a camada bentoo em ambientes isolados, compila pacotes em variações de USE, serve esses pacotes via binhost, monta as ISOs live e publica **lançamentos semanais** (cadência fixa: **todo domingo às 00:00**) — em múltiplas **arquiteturas otimizadas** e múltiplos **flavors** (desktop + init system).

### Pilares

1. **Layering, não seed chain** — por **padrão** parte de um stage3 pronto e não recompila stage1→2→3; opcionalmente (`seed_source: catalyst`) gera a seed chain microarch via Catalyst (ver §5.1).
2. **Dois subsistemas desacoplados** — *Package Factory* (compila) e *ISO Assembler* (monta).
3. **Composição em três eixos** — `arch × flavor × init`, sem explosão combinatória.
4. **Ambientes limpos por flavor** — KDE/Qt e GNOME/GTK nunca coexistem no mesmo build.
5. **Reprodutível por entrada** — pin de snapshot do `::gentoo` por release; mesmo input → mesmo conjunto de pacotes.
6. **Tronco persistente + wipe na toolchain** — semana normal reusa o binhost (delta); bump de toolchain recria tudo limpo.

---

## 2. Glossário

| Termo | Definição |
|---|---|
| **stage3** | Tarball base oficial do Gentoo (sistema mínimo + toolchain). Ponto de partida. |
| **stage4** | stage3 + pacotes/config adicionais. O artefato "sistema bentoo". |
| **flavor** | Ecossistema-alvo: `minimal` (sem DE, só TTY), `kde` (Qt), `gnome`/`xfce` (GTK), `wm` (Wayland-only: Hyprland/Sway/niri). |
| **init** | Sistema de init: `systemd` ou `openrc` (com elogind/seatd). |
| **arch** | Alvo de microarquitetura: `v3` (baseline), `znver5`, `arrowlake`. |
| **recipe** | Receita YAML componível que descreve uma release (`base + arch + flavor + init`). |
| **step** (fase) | Etapa ordenada de `emerge` (toolchain → graphics → desktop → apps); pode carregar `use_break`. |
| **use_break** | USE transiente, por step, que quebra dependência circular de *build* (≠ USE final do flavor). Curado **manualmente** por flavor. |
| **binhost** | Repositório HTTP de pacotes binários (binpkgs) servidos a clientes. |
| **multi-instance** | Recurso do Portage: múltiplos binpkgs do mesmo pacote/versão com USE diferentes. |
| **binpkg transiente** | binpkg construído só para quebrar um ciclo (ex.: `ffmpeg[-sdl]`); descartado após o *settle-pass*, nunca chega à ISO. |
| **build-pool / publish-pool** | Duas vistas do binhost: build-pool inclui transientes (reuso entre semanas); publish-pool só finais (Assembler/usuários). |
| **fork-point** | Ponto do pipeline onde o tronco compartilhado se ramifica por desktop. |
| **toolchain-bump** | Mudança de major em GCC/glibc/binutils (ou novo pin de snapshot) que dispara rebuild total limpo (`--emptytree`). |

---

## 3. Objetivos e Não-objetivos

### Objetivos
- Automação **end-to-end**: do stage3 à ISO publicada, sem intervenção manual.
- **Lançamentos semanais** fixos (domingo 00:00), sincronizados com os autobuilds do Gentoo.
- ISOs **otimizadas por microarquitetura** (Zen 5, Intel moderno) além do baseline.
- **Binhost** servindo pacotes em **variações de USE** (ex.: LibreOffice Qt vs GTK).
- **Reprodutibilidade de entrada** auditável (mesmo input → mesmo conjunto de pacotes/USE).
- Suporte a múltiplos **flavors** (minimal, KDE, GNOME, XFCE, WM) e **inits** (systemd, openrc).
- **Extensibilidade por terceiros**: "receita é dado" — qualquer pessoa adiciona sua arch/init/desktop em YAML.

### Não-objetivos (escopo explicitamente fora)
- Por **padrão** (`seed_source: download`) parte de um stage3 oficial pronto e **não** recompila a seed chain. **Opcionalmente**, por arch (`seed_source: catalyst` em `recipe.yaml`), o Shidashi *gera* um stage3 com o `-march` do alvo via **Catalyst** (recompila stage1→2→3), usando o stage3 genérico já verificado como semente de bootstrap — produzir microarquiteturas sob encomenda (仕出し). Ver §5.1.
- **Não** é um instalador gráfico (Calamares/etc. é um componente *do live medium*, não do builder).
- **Não** persegue **reprodutibilidade bit-a-bit** (ISO byte-idêntica) — apenas reprodutibilidade *de entrada*.
  Bit-a-bit em Gentoo (timestamps, build paths) custaria desproporcionalmente; fora de escopo por ora.
- **Não** é um binário standalone para host arbitrário. O Shidashi **exige um host Gentoo com Portage**
  (`import portage`) e é **distribuído como ebuild** (`app-misc/shidashi` no overlay) ou `pip install` — nunca
  como single-binary. *(É isto, e não "produto a terceiros", que fixa a linguagem em Python — ver §12.)*
- **Não** é multilib por padrão. O bentoo é **no-multilib** (puro 64-bit); 32-bit (Steam, wine, alguns
  drivers) fica para uma fase futura de **suporte avançado a jogos**, habilitado **por-pacote via
  `ABI_X86="32 64"`** em `package.use` — nunca pelo profile multilib global. Migrar `no-multilib →
  multilib` é custoso e está fora de escopo agora (ver §11 e §19).

---

## 4. Princípios de Design

1. **Receita é dado, código é burro.** Toda variação vive em arquivos YAML/Portage versionados; o orquestrador apenas executa. É isto que viabiliza terceiros criarem suas variantes sem tocar em código.
2. **Fonte única de verdade.** Cada fragmento de flavor define o USE **uma vez**, consumido tanto pela Factory (build) quanto pelo Assembler (consumo) — elimina drift.
3. **Pureza de ambiente.** Cada flavor compila no seu próprio container; closures de dependência nunca se misturam.
4. **Cache agressivo.** Fases são camadas cacheáveis (estilo Docker layers); só recompila o que mudou.
5. **Delegue ao Portage.** Profiles, resolução de deps e USE são responsabilidade do Portage — o builder não reimplementa.

---

## 5. Conceitos Centrais

### 5.1 Layering sobre stage3

```
stage3 (oficial) ──▶ [camada bentoo: config + pacotes] ──▶ stage4 bentoo ──▶ ISO
```

Diferente de Catalyst/Metro (que fazem `seed → stage1 → stage2 → stage3`), o bentoo-builder **por padrão parte de um stage3 pronto** e aplica uma camada. Modelo conceitualmente próximo ao **Calculate Linux** (`cl-builder`/`cl-image`), mas sem o acoplamento ao ecossistema Calculate.

**Fonte de seed opcional por arch (story 005).** Quando um arch declara `seed_source: catalyst`, o Shidashi inverte essa premissa *para aquele alvo*: invoca o **Catalyst** para produzir um stage3 com o `-march` específico (`seed → stage1 → stage2 → stage3`), tendo o stage3 genérico (já baixado e verificado por GPG+SHA-512) como **semente de bootstrap**. O `-march`/GOAMD64/CPU_FLAGS entram pelo `portage_confdir` do Catalyst — que reusa o `variants/arch/<arch>/portage/` existente — e não pelo `subarch` (que fica no baseline `amd64`). O stage3 resultante é pinado por SHA-512 no `BuildState` e segue pelo mesmo pipeline de extração → camada stage4. O default (`download`) permanece inalterado.

### 5.2 Os três eixos de variação

```
            arch                 flavor                init
        ┌─────────┐         ┌─────────────┐       ┌──────────┐
        │ v3       │         │ minimal     │       │ systemd  │
        │ znver5   │    ×    │ kde (Qt)    │   ×   │ openrc   │
        │ arrowlake│         │ gnome (GTK) │       └──────────┘
        └─────────┘         │ xfce (GTK)  │
                            │ wm (Wayland)│
                            └─────────────┘
```

O **desktop ≈ flavor** (KDE→Qt, GNOME/XFCE→GTK, WM→Wayland/Hyprland·Sway·niri, minimal→sem DE/só TTY), então os eixos não se multiplicam de forma ingênua. A composição evita escrever N×M×K receitas completas.

### 5.3 Dois subsistemas

```
┌─ PACKAGE FACTORY ───────────────────┐      ┌─ ISO ASSEMBLER ────────────────────┐
│ Para cada (arch × flavor × init):   │      │ Para cada (arch × init × desktop): │
│                                     │      │                                    │
│  container isolado (systemd-nspawn) │      │  container seeda stage3            │
│   ├─ aplica profile + USE do flavor │ ───▶ │   ├─ emerge --usepkgonly do         │
│   ├─ emerge em FASES (com ccache)   │binhost│   │   binhost (arch,flavor) certo   │
│   └─ produz binpkgs (multi-instance)│      │   ├─ mksquashfs (zstd)             │
│                                     │      │   ├─ dracut (dmsquash-live)        │
│  publica no binhost POR ARCH        │      │   └─ grub-mkrescue/xorriso → ISO   │
└─────────────────────────────────────┘      └────────────────────────────────────┘
       (compilação pesada, lenta)                  (seleção + empacote, rápido)
```

**Por que desacoplar:** a Factory carrega o custo de compilação (horas, com ccache/sccache); o Assembler vira quase instantâneo (`--usepkgonly`). ISO semanal fica barata.

> **Por que NÃO modelar pelos passos do Handbook do Gentoo:** o Handbook descreve uma instalação
> interativa bare-metal humana (disks, network, bootloader…), misturando *build de pacotes* e
> *montagem de imagem* numa sequência linear. O Shidashi separa esses dois mundos (Factory/Assembler) de
> propósito. O Handbook serve como **checklist de cobertura** (nenhum passo essencial esquecido),
> **não** como estrutura de execução — porque os ciclos são fenômeno de *ordem de build de pacotes*,
> não de *etapa de instalação* (ver §6.4 e §18).

---

## 6. Package Factory (detalhado)

### 6.1 Ambientes limpos por flavor

O requisito central: **a versão Qt de um pacote é compilada onde GTK/GNOME nem está instalado, e vice-versa.**

- Container `kde/qt`: profile + `USE="qt6 kde -gnome -gtk"` → closure puro Qt.
- Container `gnome/gtk`: profile + `USE="gtk gnome -qt6 -kde"` → closure puro GTK.
- Container `minimal`: USE de sistema, **sem DE / só TTY**, `-qt6 -gnome -kde -gtk` agressivo.
- Container `wm`: `wayland` + Hyprland/Sway/niri (Wayland-only), USE enxuta gráfica, **sem X11**.

A garantia de pureza vem do **container por flavor**, não do recurso de armazenamento.

### 6.2 Binpkg multi-instance

`FEATURES="buildpkg binpkg-multi-instance"` + `BINPKG_FORMAT="gpkg"` permite **N builds do mesmo pacote/versão com USE diferentes** coexistindo, distinguidos por `BUILD_ID`.

Exemplo concreto — **LibreOffice**:

```
app-office/libreoffice-X.Y[gtk,-qt6,-kde]   ← BUILD_ID 1  (consumido por GNOME/XFCE)
app-office/libreoffice-X.Y[qt6,kde,-gtk]    ← BUILD_ID 2  (consumido por KDE)
```

Quando o Assembler da ISO KDE roda `emerge --usepkgonly libreoffice`, o USE resolvido **casa** com a instância Qt → puxa a correta. A ISO GNOME casa com a GTK. **O fragmento de flavor é a fonte única que define esse USE nos dois lados.**

### 6.3 Particionamento do binhost

- **Por arch (obrigatório):** binpkgs `znver5` (com AVX-512) **não rodam** em hardware `v3`. CFLAGS/ISA incompatíveis → **um tree de binhost por arch**.
- **Dentro de cada arch:** multi-instance absorve as variações de USE (flavor + init).
- **Não se particiona por step.** O step ordena o *build*, não o *armazenamento*; separar por step destruiria o reuso entre semanas.

```
binhost/
├── v3/            (Packages index + gpkgs)
├── znver5/
└── arrowlake/
```

> **Cache de compilação é compartilhado, não segregado.** ccache (C/C++) e sccache (Rust) podem usar um
> diretório físico único entre flavors **sem violar a pureza**: o hash de cada entrada **inclui as flags**
> (`-march`, `CFLAGS`…), então `znver5` nunca colide com `v3`. A pureza é garantida pelo *container*, não
> pela segregação de cache. `mold` é linker — não gera cache; é só um knob de `RUSTFLAGS`/`LDFLAGS` por arch.

### 6.4 Build em fases (resolve dependência circular)

Cada fase é um `emerge` próprio, ordenado, com o `/etc/portage` do flavor já aplicado:

```
fase 0: stage3 base                       (já pronto)
fase 1: rebuild      emerge --newuse @world  (reflete make.conf do flavor/arch)
fase 2: graphics     @graphics  (wayland, mesa, pipewire, dbus, seat)
fase 3: desktop      @kde | @gnome | @xfce | @wm   (minimal pula esta fase)
fase 4: apps         @bentoo-apps
```

Isso resolve de forma **determinística** os ciclos circulares de pacotes grandes (ex.: KDE) — instala Wayland/toolchain primeiro, DE depois, reduzindo a área de conflito.

> **Refinamento (teste de resolução — §18):** os steps *ordenam* o build, mas o teste mostrou que alguns ciclos exigem **`use_break`** — compilar com a USE desligada e religá-la num *settle-pass* (o mesmo pacote compila duas vezes no primeiro build). A USE de *completude* (qt6/gtk/VIDEO_CARDS/L10N) é **final desde o step 1**; **só a USE de quebra-de-ciclo se estagia**, e é **curada manualmente** por flavor. Ver §18.3.

### 6.5 Cache de fases (fork-point)

As fases iniciais dependem **só do init**, não do desktop. O fork ocorre tarde:

```
stage3 ─ rebuild ─ seat ─ graphics ──┬── (minimal: para aqui) ─▶ binpkgs minimal
        (por init)                   ├── +kde     ─▶ binpkgs kde
                                     ├── +gnome   ─▶ binpkgs gnome
                                     ├── +xfce    ─▶ binpkgs xfce
                                     └── +wm      ─▶ binpkgs wm (Hyprland/Sway/niri)
```

Snapshot (btrfs subvol ou tarball) no fork-point: o tronco compila **uma vez por init**; só a fase de desktop ramifica. Builds semanais reusam o tronco quando a receita não mudou. O flavor `minimal` é praticamente o próprio tronco — não tem fase de desktop.

### 6.6 Estratégia de build: tronco persistente + wipe na toolchain

Duas situações, duas estratégias (resolve a tensão "build barato" × "build limpo"):

| Situação | Estratégia |
|---|---|
| Semana normal (bumps de pacote) | **Tronco persistente** → `--newuse`/delta sobre o binhost. Rápido. |
| **toolchain-bump** (GCC/glibc/binutils major **ou** novo pin de snapshot) | **Wipe total + `--emptytree`** → recompila tudo do zero, **sem resíduo**. |

A detecção de toolchain-bump dispara também `@preserved-rebuild` e os subslot-rebuilds que o Portage sinaliza — para não restar binpkg linkado contra ABI antiga. Quando o GCC muda, **nada é mesclado**: o binhost novo é regenerado limpo, e o pin de snapshot garante que releases distintas nunca cruzem pacotes. (Ver §10 e §18.3.)

---

## 7. ISO Assembler (detalhado)

| Etapa | Ferramenta | Notas |
|---|---|---|
| Seed do rootfs | stage3 + `emerge --usepkgonly` | Puxa tudo do binhost; não compila |
| Compressão | `mksquashfs` (zstd -19) | rootfs read-only |
| Live boot | **dracut** módulo `dmsquash-live` | overlayfs em RAM, padrão moderno |
| Bootloader | `grub-mkrescue` / `xorriso` | ISO híbrida BIOS + UEFI |
| Pós-processo | checksum SHA256 + assinatura GPG | publicação |

> Observação: o sistema *instalado* pode usar dist-kernel + UKI (como no `make.conf` de referência), mas o *live medium* usa dracut `dmsquash-live` clássico.

> **O Assembler é imune a ciclo (§18.6):** `--usepkgonly` instala binário pronto, **sem ordem de build** — o ciclo é fenômeno de *build-time*, resolvido na Factory. O Assembler apenas navega o grafo multi-instance e extrai a fatia do seu flavor pela **USE final** (não "o último compilado"). Toda a complexidade de ciclo fica na Factory.

---

## 8. Matriz de Variação

### Flavors × Desktops × Inits

| Flavor | Desktops | USE característica | Init compatível |
|---|---|---|---|
| `minimal` | **nenhum (console-only)** | `-qt6 -gnome -kde -gtk` (sistema base) | systemd / openrc |
| `kde` | KDE Plasma | `qt6 kde wayland -gnome -gtk` | systemd / openrc |
| `gnome` | GNOME | `gtk gnome wayland -qt6 -kde` | systemd / openrc |
| `xfce` | XFCE | `gtk -qt6 -gnome` | systemd / openrc |
| `wm` | **Hyprland · Sway · niri** (Wayland-only) | `wayland -qt6 -gnome -kde` | systemd / openrc |

> **`minimal` = stage4 base, sem ambiente gráfico, apenas TTY** (modelo Arch/Debian-netinst). É o "controle" mais
> limpo contra a ISO KDE no piloto, e praticamente o tronco do fork-point — **sem compositor**, nem para smoke-test.
> **`wm` = Wayland-only**: **Hyprland** (default, tiling dinâmico) + **Sway** (i3-compatível) + **niri**
> (tiling *scrollable*) — o trio mais usado/moderno em distros, sem nenhuma dependência X11
> (coerente com a base `wayland` + `seatd`/`elogind`). Vitrine, separada do minimal por carregar mais dependências.

### Acoplamento init ↔ seat (a parte não-ortogonal)

| | systemd | openrc |
|---|---|---|
| logind | `systemd` (nativo) | **`elogind`** |
| seat (WM/Wayland) | systemd-logind | elogind + `seatd` |
| USE global | `systemd -elogind` | `elogind -systemd` |

O fragmento de **init** carrega o USE de seat; os fragmentos de **desktop** assumem "seat já resolvido" e permanecem agnósticos ao init.

---

## 9. Otimização por Arquitetura

### 9.1 Tabela de alvos

| Alvo | `-march` | `GOAMD64` | `RUSTFLAGS target-cpu` | AVX-512 |
|---|---|---|---|---|
| **baseline** | `x86-64-v3` | `v3` | `x86-64-v3` | não |
| **Zen 5** | `znver5` | `v4` | `znver5` | **sim** |
| **Arrow Lake** | `arrowlake` | `v3` | `arrowlake` | **não** |

### 9.2 ⚠️ Armadilha do `x86-64-v4` para Intel

`x86-64-v4` **exige** AVX-512, mas Intel **client** (Alder Lake em diante) **removeu** AVX-512. Uma ISO `-march=x86-64-v4` **não roda** em Arrow Lake. Para "Intel moderno", usar o `-march` específico (`arrowlake`), **nunca v4**. `v4` só serve para Intel **server** (Granite Rapids) ou Zen 4/5.

### 9.3 Knobs que precisam andar JUNTOS

O fragmento de `arch` deve parametrizar **todos** os controles de CPU em conjunto, senão um Go/Rust v3 vaza num binpkg znver5:
- `COMMON_FLAGS` (CFLAGS/CXXFLAGS/…)
- `GOAMD64`
- `RUSTFLAGS -C target-cpu`
- `CPU_FLAGS_X86` — **setado manualmente por alvo** (não usar `cpuid2cpuflags`, que detecta o host)
- `CHOST` (quando aplicável)

### 9.4 Host de build atual e tiers de validação

Host: **AMD Ryzen 9 9950X (Zen 5, classe v4, com AVX-512).** Consequências:
- **Constrói** qualquer alvo (GCC 16 cross-compila znver5/arrowlake sem problema).
- **Roda nativamente** binpkgs `v3` **e `znver5`** (o Zen 5 tem AVX-512) → ambos são boot-testáveis no host.
- **Não roda** `arrowlake`: `-march=arrowlake` pode usar ISA Intel-específica ausente no AMD → SIGILL.

**Estratégia de teste por tier:**

| Tier | Archs | Validação |
|---|---|---|
| Tier 1 | `v3`, **`znver5`** | Smoke-test + boot nativo no host (9950X) |
| Tier 2 | `arrowlake` | **Boot-test via QEMU (TCG)** — build-only no host AMD; sem hardware Intel real |

> Mudança vs. host anterior (Zen 3): **`znver5` subiu para Tier 1** — deixa de ser build-only e passa a
> ser validável nativamente, porque o 9950X possui AVX-512.
>
> **`arrowlake` valida só por QEMU (TCG):** como Arrow Lake **não tem AVX-512**, sua ISA cabe na emulação
> TCG — boot-test fiel o bastante sem aquisição de hardware Intel.

---

## 10. Reprodutibilidade

- **Nível perseguido: reprodutibilidade de *entrada*** (mesmo input → mesmo conjunto de pacotes/USE), **não** bit-a-bit. O objetivo é **replicar o processo sem erros que quebrem**, não gerar ISO byte-idêntica.
- **Pin de snapshot do `::gentoo`** por release (squashfs datado) → input determinístico.
- **`-march=native` é banido** (§9.3) — pré-requisito de qualquer determinismo; sempre `-march` explícito por arch.
- Tensão conhecida: o sistema usa `ACCEPT_KEYWORDS="~amd64"` (testing, muda rápido). O pin do snapshot é **ainda mais crítico** nesse contexto — não é opcional.
- Cada release registra: hash do stage3, hash do snapshot do repo, hash das receitas, versões do toolchain.
- Saída versionada: `bentoo-<flavor>-<init>-<arch>-<data>.iso` + `.sha256` + `.asc`.

---

## 11. Fluxo de Release Semanal

Cadência fixa: **todo domingo às 00:00.**

**Seed por init (no-multilib).** O Shidashi semeia cada variante a partir do stage3
**no-multilib** do init correspondente — **não** do tarball `desktop` (que é multilib).
Trocar de init não se faz por conversão de profile (operação "difícil" segundo o Handbook):
cada init parte do seu próprio stage3.

| init | stage3 seed (autobuild oficial) | profile-base âncora |
|---|---|---|
| systemd | `stage3-amd64-nomultilib-systemd-<snapshot>.tar.xz` | `default/linux/amd64/23.0/no-multilib/systemd` |
| openrc  | `stage3-amd64-nomultilib-openrc-<snapshot>.tar.xz`  | `default/linux/amd64/23.0/no-multilib` |

> Snapshot do piloto: **`20260517T170110Z`** (ambos os inits do mesmo snapshot, para reprodutibilidade).

```
┌─ trigger (cron: domingo 00:00) ───────────────────────┐
│ CI lê os pointer files dos autobuilds (no-multilib):  │
│   .../latest-stage3-amd64-nomultilib-systemd.txt      │
│   .../latest-stage3-amd64-nomultilib-openrc.txt       │
│ Compara cada um com o último build registrado.        │
└──────────────────┬────────────────────────────────────┘
                   │ mudou? (toolchain-bump? → wipe; senão → delta)
                   ▼
        ┌─ Factory (matriz arch × flavor × init) ─┐
        │ rebuild dos binpkgs alterados            │
        └──────────────────┬───────────────────────┘
                           ▼
        ┌─ Assembler (matriz arch × init × desktop) ─┐
        │ re-spin das ISOs a partir do binhost        │
        └──────────────────┬──────────────────────────┘
                           ▼
        ┌─ Publish: checksum + GPG + upload ──────────┐
        └─────────────────────────────────────────────┘
```

CI como matriz (exemplo conceitual):

```yaml
on:
  schedule:
    - cron: "0 0 * * 0"   # domingo 00:00 UTC
strategy:
  matrix:
    arch:    [v3, znver5, arrowlake]
    init:    [systemd, openrc]
    desktop: [minimal, kde, gnome, xfce, wm]
```

---

## 12. Stack Tecnológica

### Linguagem: **Python ≥ 3.14** (definitivo)

| Argumento | Detalhe |
|---|---|
| **Portage é uma biblioteca Python** | `import portage`: consulta a árvore, resolve átomos, lê profiles, parseia o índice `Packages`, manipula metadados de binpkg multi-instance — **sem shell out + parse de texto** que Go/Rust exigiriam. Catalyst e Metro usam essa API. |
| **O alvo SEMPRE tem Portage** | O Shidashi roda num host Gentoo. Não existe "Shidashi single-binary em host arbitrário" — Go não removeria a dependência do Portage. Distribuir = **ebuild `app-misc/shidashi`** ou `pip`. |
| **Glue subprocess-bound** | O trabalho pesado é do `emerge`; performance da linguagem é irrelevante. Domina a **velocidade de iteração** (receitas mudam toda semana). |
| **Rigor recuperável** | `pydantic` (schema das receitas) + `mypy --strict` + `ruff` cobrem a segurança de tipo onde o erro dói. |
| **Cresce sem reescrever** | Dashboard/servidor de binhost cabem em FastAPI + asyncio; o núcleo permanece. |

> **Rust** foi descartado: otimiza a correção da camada onde os bugs *não* estão (USE flag/`emerge`/shell), ao maior custo de iteração. **Go** só venceria num orquestrador remoto que não tocasse Portage local — e isso é o *dashboard* (FastAPI já cobre).

**Recursos modernos de Python 3.14 a explorar:**
- **PEP 695** — sintaxe nova de genéricos e type alias (`type Recipe = ...`, `def merge[T](...)`) nos modelos de receita.
- **PEP 749** — anotações *lazy* por padrão → menos custo de import, ótimo para pydantic.
- **PEP 750 — t-strings** — montagem **segura contra injeção** de comandos `emerge`/shell no `container.py`.
- **`match`** — despacho do deep-merge dos eixos e do parse dos resultados do `pretend-resolve`.
- **`tomllib`** (stdlib) — leitura de TOML sem dependência extra.

### Componentes

| Função | Escolha |
|---|---|
| Orquestrador | Python 3.14 + pydantic + mypy + ruff |
| Helpers no container | Bash (`emerge`, `eselect`) |
| Isolamento | `systemd-nspawn` |
| Cache de fase | btrfs subvol / tarball |
| Cache de compilação | ccache (C/C++) + sccache (Rust) — **compartilhado** entre flavors |
| Linker | mold (knob por arch) |
| Compressão squashfs | zstd |
| Live boot | dracut `dmsquash-live` |
| ISO | `grub-mkrescue` / `xorriso` |
| Binpkg | gpkg + multi-instance + assinatura GPG |
| Distribuição do Shidashi | ebuild `app-misc/shidashi` (overlay) |
| CI | GitHub Actions (runner grande) ou self-hosted |

### Ambiente verificado (host atual)
`GCC 16.1.0` · `Clang 22.1.6` · `mold 2.41` · `Portage 3.0.79` · `Python 3.14.5` · `Go 1.26.3` · `Rust 1.95.0` · profile `no-multilib/systemd` · CPU **Ryzen 9 9950X (Zen 5)**. GCC suporta todos os `-march` alvo.

---

## 13. Layout do Repositório

Co-localizado **por eixo** (`variants/<eixo>/<nome>/`): tudo de uma variante numa pasta só — facilita adicionar/remover uma variante inteira (e contribuições de terceiros).

```
stages/                          # raiz do projeto (este repo)
├── OVERVIEW.md                  # este documento
├── README.md · pyproject.toml · .gitignore
├── shidashi/                        # pacote Python (orquestrador)
│   ├── cli.py                   # CLI Typer (subcomandos)
│   ├── config.py                # caminhos/paths
│   ├── recipe.py                # modelos pydantic + merge dos eixos
│   ├── container.py             # wrapper systemd-nspawn
│   ├── factory.py               # subsistema Package Factory
│   ├── assembler.py             # subsistema ISO Assembler
│   ├── phases.py                # execução de fases + cache de camadas
│   ├── binhost.py               # gestão multi-instance + índice + assinatura
│   ├── image.py                 # squashfs + dracut + ISO
│   └── portage_api.py           # integração com `import portage`
├── variants/                    # eixos componíveis (recipe + portage + sets juntos)
│   ├── base/                    # comum a tudo
│   │   ├── base.yaml
│   │   ├── portage/             # /etc/portage base (CORE/FEATURES/DISTDIR/PKGDIR…)
│   │   └── sets/
│   ├── arch/
│   │   ├── v3/{recipe.yaml, portage/}
│   │   ├── znver5/{recipe.yaml, portage/}
│   │   └── arrowlake/{recipe.yaml, portage/}
│   ├── flavor/
│   │   ├── minimal/{recipe.yaml, portage/, sets/}
│   │   ├── kde/{recipe.yaml, portage/, sets/}
│   │   ├── gnome/{recipe.yaml, portage/, sets/}
│   │   ├── xfce/{recipe.yaml, portage/, sets/}
│   │   └── wm/{recipe.yaml, portage/, sets/}     # Wayland-only: Hyprland, Sway, niri
│   └── init/
│       ├── systemd/{recipe.yaml, portage/}
│       └── openrc/{recipe.yaml, portage/}
├── seeds/stage3.toml            # pointer pinado do stage3 (§10/§11)
├── scripts/
│   └── postinstall.d/           # customizações idempotentes
└── .github/workflows/release.yml

# overlay bentoo: externo a este repo, em /var/db/repos/bentoo
```

**Mapeamento de sets → eixo** (os sets que as fases do §6.4 consomem):

| Set | Local | Escopo |
|---|---|---|
| `graphics`, `bentoo-apps` | `variants/base/sets/` | **compartilhados** por todos os flavors (fases 2 e 4) |
| `kde`, `gnome`, `xfce`, `wm` | `variants/flavor/<f>/sets/` | **específicos** do desktop (fase 3) |

O `minimal` não tem set de desktop — consome apenas os sets de `base`. Cada fragmento (`recipe.yaml`,
`portage/`, `sets/`) é resolvido pelo `shidashi recipe` via deep-merge na ordem `base → arch → flavor → init`.

---

## 14. Schema de Receita (exemplo)

Uma release é a tupla `base + arch + flavor + init`, resolvida por *deep-merge*:

```yaml
# variants/flavor/kde/recipe.yaml
flavor: kde
# sem profile de DE: a âncora é só no-multilib[/systemd]; a "camada KDE"
# vem de use_prefer + portage/package.use + sets (ver "Resolução de profile" abaixo).
use_prefer:
  add:  [qt6, kde, wayland]
  drop: [gtk, gnome, webkit]
sets:
  - graphics
  - kde
override_ok: false                    # KDE é curado; WM teria true
```

```yaml
# variants/flavor/minimal/recipe.yaml
flavor: minimal
use_prefer:
  drop: [qt6, kde, gnome, gtk]        # console-only (só TTY)
sets: []                              # nenhum set de desktop — consome só os de base
override_ok: true
```

```yaml
# variants/arch/znver5/recipe.yaml
arch: znver5
common_flags: "-march=znver5 -O2 -pipe"
goamd64: v4
rustflags: "-C target-cpu=znver5 -C link-arg=-fuse-ld=mold"
cpu_flags_x86: [aes, avx, avx2, avx512f, avx512bw, avx512cd, avx512dq,
                avx512vl, avx512vbmi, avx512vbmi2, vaes, vpclmulqdq, gfni, sha]
runnable_on_build_host: true          # tier 1 no 9950X (Zen 5 tem AVX-512)
```

```yaml
# variants/init/openrc/recipe.yaml
init: openrc
profile_suffix: ""                    # profile sem /systemd
use_prefer:
  add:  [elogind, udev]
  drop: [systemd]
phases_prepend:
  - { name: seat, packages: [sys-auth/elogind, sys-auth/seatd] }
```

**Semântica de `override_ok`.** Controla se camadas posteriores do deep-merge (o fragmento de
`init` e, no futuro, receitas de usuário) podem **sobrescrever/derrubar** a USE curada do flavor:
- `false` (kde, gnome, xfce — curados): a `use_prefer` do flavor é **autoritativa**; o merge
  **rejeita** drops/overrides conflitantes vindos de baixo → ISOs previsíveis e reproduzíveis.
- `true` (wm, minimal — livres): camadas posteriores **podem** ajustar a USE, habilitando
  customização (o usuário do `wm` troca compositor/USE sem precisar forkar a receita).

**Resolução de profile (decisão: `no-multilib` apenas, tudo acima por USE).** O único eixo que toca o
profile é o **init**. *Todos* os flavors herdam a mesma âncora; **não há profile de DE**:

    default/linux/amd64/23.0/no-multilib[/<init.profile_suffix>]

- `base` fixa `default/linux/amd64/23.0/no-multilib` (no-multilib é padrão — §3).
- `init.profile_suffix` acrescenta `/systemd` (systemd) ou nada (openrc).
- **O flavor NÃO contribui com profile.** A "camada de desktop" (KDE/GNOME/XFCE/WM) é construída
  inteiramente **acima** do no-multilib via `use_prefer` → `portage/package.use` + `sets` (§13).

Profile resolvido (idêntico para minimal/kde/gnome/xfce/wm — só muda por init):
- `* + openrc`  → `default/linux/amd64/23.0/no-multilib`
- `* + systemd` → `default/linux/amd64/23.0/no-multilib/systemd`

> ⚠️ **Por que não há profile de desktop.** Na árvore oficial, `desktop/*` e `no-multilib` são **irmãos**
> de `default/linux/amd64/23.0/` — não existe `no-multilib/desktop/plasma`, então não compõem. Em vez de
> criar profiles próprios no overlay, o bentoo fica **só no profile `no-multilib[/systemd]`** e codifica
> toda a diferenciação de DE via `portage/package.use` + sets (que já são load-bearing, §18.2). Vantagem:
> nada de profiles custom para terceiros manterem; a receita (`use_prefer`/sets) é a **única** fonte da
> camada gráfica. (Decisão §19.1.)

---

## 15. Decomposição do make.conf

O `make.conf` de referência (já organizado em grupos nomeados) mapeia diretamente:

| Grupo no make.conf | Fragmento de destino |
|---|---|
| `CORE KERNEL COMPRESSOR GRAPHICS DEVELOPMENT PERFORMANCE FILESYSTEM IMAGE AUDIO VIDEO NETWORK DEVICES SECURITY VIRTUALIZATION` | `variants/base/portage/` |
| `COMMON_FLAGS GOAMD64 RUSTFLAGS CPU_FLAGS_X86 CHOST` | `variants/arch/<x>/portage/` |
| `DESKTOPS REMOVED` (parte gráfica) | `variants/flavor/<y>/portage/` |
| `SYSTEMD` / elogind | `variants/init/<z>/portage/` |
| `FEATURES DISTDIR PKGDIR ccache/sccache` | `variants/base/portage/` |

> A primeira release do bentoo é, essencialmente, o make.conf atual fatorado em
> `base + arch/v3 + flavor/kde + init/systemd` (e `flavor/minimal`). **Não se cria do zero —
> fatora-se o existente** (com menos USE que o exemplo).

---

## 16. Binhost: Segurança e Distribuição

- **Assinatura obrigatória se público:** gpkg suporta assinatura GPG nativa. Ligar `binpkg-request-signature` nos clientes. Binhost público sem assinatura é vetor de supply-chain.
- **Índice por arch:** cada `binhost/<arch>/Packages` é independente.
- **Hospedagem evolutiva (local → online):** dev usa binhost **local/self-hosted** no próprio host (custo zero); a fase pública migra para **Cloudflare R2** (object storage S3-compatível, **sem taxa de egress**) para binhost e ISOs.
- **Bônus:** o mesmo binhost que alimenta o Assembler pode servir os **usuários finais** do bentoo (modelo Redcore/Sisyphus), acelerando instalações.

---

## 17. Roadmap de Desenvolvimento

> Legenda: `[x]` implementado e validado pela suíte off-host · `[ ]` pendente.
> *Pilots de build/boot em host Gentoo root são host-gated e seguem diferidos
> mesmo onde o código está completo (stories 003/004); anotados inline.*

### Fase 0 — Fundação (MVP)
- [x] Esqueleto Python (≥3.14) + pydantic + estrutura de receitas por eixo.
- [x] `recipe.py` + `cli.py`: `recipe show/validate` (deep-merge dos eixos) — primeiro entregável real.
- [x] `shidashi pretend <arch> <flavor> <init>` (descoberta de ciclos, custo segundos).
- [x] Wrapper `systemd-nspawn`.
- [x] Detector de stage3 (pointer file).
- [x] Pipeline mínimo: **`v3 × minimal × systemd`** → stage4 tarball, depois `v3 × kde × systemd`. *(`shidashi factory` completo + testado; pilot de build em host root diferido — stories 003/004.)*

### Fase 1 — ISO
- [x] Assembler: squashfs + dracut `dmsquash-live` + ISO híbrida. *(impl. + testes off-host; caminho de imagem validado por boot real — pilot de montagem via binhost da Fase 2 ainda diferido.)*
- [x] Smoke-test de boot (QEMU + nativo no 9950X) automatizado. *(`scripts/smoke-iso.sh` + `tests/test_smoke_iso.py` host-gated; **boot validado em QEMU/KVM no 9950X** — grub → dmsquash-live monta o squashfs → systemd switch-root → userspace.)*

### Fase 2 — Binhost & Factory
- [x] Factory com fases + cache de fork-point. *(impl. + testes; pilot host root diferido.)*
- [ ] Estratégia tronco-persistente / wipe-na-toolchain (§6.6) + fase toolchain-bump.
- [ ] Binhost multi-instance + assinatura.
- [ ] LibreOffice Qt vs GTK como prova de conceito. *(variante Qt presente em `flavor/kde`; contraparte GTK pendente.)*

### Fase 3 — Matriz
- [x] Eixo arch: `znver5` (tier 1), `arrowlake` (tier 2, build-only, boot-test QEMU/TCG). *(receitas v3/znver5/arrowlake completas; boot-test `arrowlake` QEMU/TCG diferido.)*
- [ ] Eixo flavor: `gnome`, `xfce`, `wm` (Wayland-only: Hyprland, Sway, niri). *(só `kde` e `minimal` curados; `gnome`/`xfce`/`wm` ainda placeholder — sets vazios.)*
- [x] Eixo init: `openrc`. *(systemd + openrc completos.)*

### Fase 4 — Automação
- [ ] CI matriz semanal (cron domingo 00:00) + publicação + checksums/GPG. *(scaffold gated com `if: false`; falta runner Gentoo + leitura de pointer file + comando `release`.)*
- [ ] Pin de snapshot reprodutível. *(pin do stage3 seed pronto; pin do snapshot `::gentoo` por release pendente.)*

### Fase 5 — Operação (opcional)
- [ ] Dashboard de releases (FastAPI).
- [ ] Binhost público para usuários finais (Cloudflare R2).
- [ ] ebuild `app-misc/shidashi` no overlay.

---

## 18. Validação Empírica e Refinamentos (teste de resolução)

> Registra o que um **teste de resolução** (`emerge --pretend`, **sem compilar**) revelou
> sobre o pipeline real, e os refinamentos de arquitetura que decorreram dele.
> Ferramenta: `shidashi pretend <arch> <flavor> <init>` — pipeline `seed → apply_portage → run`.

### 18.1 Metodologia

Antes de gastar horas compilando, resolve-se a árvore com `emerge --pretend --emptytree`
dentro de um stage3 semeado, medindo **a lista de pacotes** e detectando **dependências
circulares** — sem build. Cenário **T0** = make.conf de referência com USE completa de uma
vez ("tudo de uma vez", estilo naive). Custo ~segundos; risco zero. Alvo do piloto:
`v3 × kde × systemd`, profile `no-multilib/systemd`, stage3 `nomultilib-systemd`.

### 18.2 Achados do T0

1. **make.conf sozinho NÃO resolve.** Só o make.conf (sem `package.use`) num stage3 limpo
   faz o resolvedor exigir uma batelada de mudanças de USE (`systemd policykit`,
   `qt5compat qml`, `kconfig qml`, `qtbase libproxy`…). **O `package.use` curado é
   load-bearing**, não cosmético — reforça §4.2 (fonte única de USE).
2. **O overlay é parte da resolução.** Sem o overlay bentoo (`/var/db/repos/bentoo`),
   pacotes-folha atrasados na migração do Python (ex.: `libffado` × `python3.14`) batem em
   `REQUIRED_USE` que o overlay já corrige. → Factory **e** Assembler devem configurar
   **todos** os repos do alvo (gentoo + bentoo + …), não só o `::gentoo`.
3. **A dependência circular é real e concreta:**
   ```
   libsdl2 ─▶ pipewire ─▶ ffmpeg ─▶ libsdl2        (build-time)
   quebra:  ffmpeg -sdl  |  libsdl2 -pipewire  |  pipewire -ffmpeg
   ```
   Confirma: USE completa de uma vez **trava em ciclo**; o resolvedor **lista** as quebras
   mas **não as aplica sozinho** — a Factory precisa codificá-las.
4. **A primeira barreira não é o ciclo — é a curadoria de USE.** A resolução morreu na USE
   antes de chegar ao ciclo; só com `package.use` + overlay o ciclo apareceu.

### 18.3 Disciplina de USE estagiada

- **USE de *completude* (qt6/gtk/kde/gnome/VIDEO_CARDS/L10N): final e global desde o step 1.**
  Não causam ciclo e não incham `@system` (que não as referencia). Estagiá-las é o que cria
  **duplicação espúria** de deps — evitar.
- **USE de *quebra-de-ciclo* (`use_break`, ex.: `ffmpeg -sdl`): transiente, por step,
  escopada via `package.use`.** É a única que se estagia: *break-pass* (USE off) →
  *settle-pass* (USE on, com o parceiro do ciclo já presente). O mesmo pacote compila duas
  vezes **no primeiro build** — custo pago uma vez, amortizado no binhost. **Curada manualmente.**
- `--newuse` **não** recompila por mudança de `-march`/CFLAGS → o primeiro build do tronco
  usa `--emptytree`; semanas seguintes são o **delta** sobre o binhost persistente. Um
  **toolchain-bump** força novo `--emptytree` (§6.6).

### 18.4 Binpkg transiente — categoria nova

O `ffmpeg[-sdl]` do break-pass é um **binpkg transiente**: existe só para quebrar o ciclo,
é substituído pelo `ffmpeg[sdl]` final, e **nunca deve chegar à ISO/usuário**. Três
multiplicidades convivem no pool multi-instance — e o GC precisa distingui-las:

| Tipo | Exemplo | Política de GC |
|---|---|---|
| Variante de flavor (legítima) | `poppler[qt6]` vs `poppler[gtk]` | manter **todas** (cada uma vai pra sua ISO) |
| Versão antiga | `poppler-24` vs `poppler-25` | keep-N mais recentes |
| Transiente de bootstrap | `ffmpeg[-sdl]` | **podar** após settle / só no build-pool |

→ `binhost.gc()` é **ciente da USE** (qual instância é a final de cada flavor), **não**
"keep-N global" (senão poda uma variante de flavor achando que é duplicata).

### 18.5 Binhost é um grafo, não um array

O binhost por arch é um conjunto de nós `(CPV, USE)` — não "o último compilado". Cada
flavor é uma **fatia consistente** desse grafo. A divergência entre flavors vem de (a) a
USE global do flavor tocar dezenas de pacotes **diretamente** e (b) **propagação por
USE-dep** (`kio[qt6]` força `qtbase[qml]`…). Pacotes sem USE de DE (toolchain, libs base)
são **idênticos** entre flavors → uma instância compartilhada (o tronco do fork-point).

**Dois níveis de pool:**
- **build-pool** — inclui transientes; reusado entre semanas para não re-quebrar ciclos.
- **publish-pool** — só finais; consumido por Assembler e usuários (índice `Packages`
  filtrado). É o que reencontra a ideia de "binhost em camadas" (tronco vs flavor).

### 18.6 O Assembler é imune a ciclo

Ciclo é fenômeno de *build-time*. `emerge --usepkgonly` **instala binário pronto**, sem
ordem de compilação → o ciclo não existe no assemble. O Assembler resolve a **USE final** e
o match multi-instance pega a instância certa por flavor (`poppler[qt6]` p/ KDE, `[gtk]` p/
GNOME). **Toda a complexidade de ciclo fica na Factory; o Assembler permanece trivial.**
Requisito: a USE final do *settle-pass* da Factory **==** a USE que o Assembler resolve
(senão `--usepkgonly` falha sem match). Vale um teste automatizado que compare a USE
prometida pelo fragmento com a gravada no `BUILD_ID`.

### 18.7 `shidashi pretend` como descobridor de ciclos

O comando `shidashi pretend <arch> <flavor> <init>` é promovido a **instrumento de curadoria**:
roda o `emerge --pretend` dentro do container, colhe as quebras sugeridas ("break this cycle
by changing USE X") e **alimenta manualmente** o `use_break` dos steps da recipe. Deixa de ser
só teste e vira parte do pipeline de curadoria.

---

## 19. Decisões

### 19.1 Tomadas (registradas)

| Tema | Decisão |
|---|---|
| **Estrutura do pipeline** | Fases-de-emerge (Factory) + Assembler. Handbook = checklist, não estrutura (§5.3). |
| **Re-seed vs. tronco** | **Híbrido:** tronco persistente para delta semanal; **wipe total `--emptytree`** em toolchain-bump (§6.6). |
| **`use_break`** | Curadoria **manual** por flavor, alimentada pelo `pretend-resolve` (§18.7). |
| **Determinismo** | De **entrada/configuração** (replicar sem erro), **não** bit-a-bit (§10). |
| **Distribuição do Shidashi** | **ebuild** `app-misc/shidashi` em host Gentoo; nunca single-binary (§3, §12). |
| **`minimal`** | Flavor **console-only, apenas TTY** — sem compositor, nem para smoke-test (§8). |
| **`wm`** | **Wayland-only:** Hyprland (default) + Sway + niri — sem dependências X11 (§8). |
| **Layout** | Co-localizado **por eixo** (`variants/<eixo>/<nome>/`); overlay externo (§13). |
| **Host de build** | **Ryzen 9 9950X (Zen 5)** → `v3` e `znver5` em Tier 1; `arrowlake` Tier 2 (§9.4). |
| **Validação `arrowlake`** | **QEMU (TCG)** — sem AVX-512, a ISA cabe na emulação; sem hardware Intel real (§9.4). |
| **Cadência** | Release fixo **todo domingo 00:00** (§11). |
| **multilib** | **no-multilib** por padrão; 32-bit só na futura fase de jogos, **por-pacote via `ABI_X86="32 64"`** (§3). |
| **Seed** | stage3 **no-multilib** por init (systemd/openrc), do mesmo snapshot pinado (§11). |
| **`override_ok`** | `false` = USE do flavor autoritativa (kde/gnome/xfce); `true` = customizável (wm/minimal) (§14). |
| **Camada de desktop** | **Profile `no-multilib[/systemd]` apenas** — sem profiles de DE no overlay; KDE/GNOME/XFCE/WM construídos **acima** via `package.use` + sets (§14). |
| **Linguagem** | **Python ≥ 3.14** (em vias de virar o padrão do Gentoo), recursos modernos (PEP 695/749/750, `match`) (§12). |
| **Hospedagem** | **Local agora → Cloudflare R2 depois** (sem egress) para binhost e ISOs (§16). |
| **Toolchain-bump** | Fase explícita que dispara `@preserved-rebuild` + subslot-rebuilds (§6.6). |

### 19.2 Ainda em aberto

**Nenhum item de arquitetura em aberto.** Todas as decisões anteriores foram fechadas e migradas
para §19.1: WMs do `wm` (Wayland-only: Hyprland/Sway/niri), validação `arrowlake` (QEMU/TCG),
`minimal` (apenas TTY), hospedagem (Cloudflare R2), multilib da fase de jogos (por-pacote via
`ABI_X86="32 64"`) e a camada de desktop (**profile `no-multilib[/systemd]` apenas**, DE por
`package.use` + sets).

Próximas decisões surgem na implementação (Fase 0): nomes/versões exatos dos pacotes por set,
política de GC do binhost e formato do pin de snapshot.

---

## 20. Referências

- [Catalyst — Gentoo Wiki](https://wiki.gentoo.org/wiki/Catalyst)
- [Funtoo Metro](https://github.com/funtoo/metro) · [stage4.spec](https://github.com/funtoo/metro/blob/master/targets/gentoo/stage4.spec)
- [Calculate Linux — Interactive system build](https://old.calculate-linux.org/main/en/interactive_system_build)
- [Distributions based on Gentoo — Gentoo Wiki](https://wiki.gentoo.org/wiki/Distributions_based_on_Gentoo)
- [Handbook:AMD64 — Gentoo Wiki](https://wiki.gentoo.org/wiki/Handbook:AMD64) (usado como checklist de cobertura)
- dracut `dmsquash-live`, `app-cdr/livecd-tools`, `binpkg-multi-instance` (Portage docs)
