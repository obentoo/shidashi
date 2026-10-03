# Shidashi 仕出し — Development Proposal

> Vision and architecture document for **Shidashi**, the build and ISO automation tool of **bentoo**.
> Status: **Phases 0 and 1 complete** (scaffold + recipe + `pretend` + `factory` + Assembler/ISO, validated off-host; ISO boot validated on QEMU/KVM; root-host build pilots deferred) · **Phase 2 (phased Factory + binhost) in progress** · Language: **Python ≥ 3.14** · Last updated: 2026-07-05

---

## 1. Executive Summary

**bentoo** is a Gentoo-derived distribution — formally a *stage4*, nicknamed *"stage5"* — built **on top of an official stage3**, with curated configuration and an additional set of packages.

**Shidashi** (`仕出し`, "catering — produces batches to order and delivers them") is the tool that automates the whole cycle: it detects the latest stage3, applies the bentoo layer in isolated environments, compiles packages in USE variations, serves those packages via a binhost, assembles the live ISOs and publishes **weekly releases** (fixed cadence: **every Sunday at 00:00**) — across multiple **optimized architectures** and multiple **flavors** (desktop + init system).

### Pillars

1. **Layering, not a seed chain** — it starts from a ready-made stage3 and does not recompile stage1→2→3 (see §5.1).
2. **Two decoupled subsystems** — *Package Factory* (compiles) and *ISO Assembler* (assembles).
3. **Composition along three axes** — `arch × flavor × init`, without combinatorial explosion.
4. **Clean environments per flavor** — KDE/Qt and GNOME/GTK never coexist in the same build.
5. **Reproducible by input** — `::gentoo` snapshot pin per release; same input → same set of packages.
6. **Persistent trunk + wipe on toolchain** — a normal week reuses the binhost (delta); a toolchain bump rebuilds everything clean.

---

## 2. Glossary

| Term | Definition |
|---|---|
| **stage3** | Official Gentoo base tarball (minimal system + toolchain). Starting point. |
| **stage4** | stage3 + additional packages/config. The "bentoo system" artifact. |
| **flavor** | Target ecosystem: `minimal` (no DE, TTY only), `kde` (Qt), `gnome` (GTK), `wm` (Wayland-only: Hyprland/Sway/niri). |
| **init** | Init system: `systemd` or `openrc` (with elogind/seatd). |
| **arch** | Microarchitecture target: `v3` (baseline), `znver5`, `arrowlake`. |
| **recipe** | Composable YAML recipe describing a release (`base + arch + flavor + init`). |
| **stage** / phase | One step of the `base → minimal → desktop → <flavor>` chain (D24); each stage becomes an `emerge` phase, with its own config, sets and `use_break`. |
| **use_break** | Transient, per-step USE that breaks a *build* circular dependency (≠ the flavor's final USE). Curated **manually** per flavor. |
| **binhost** | HTTP repository of binary packages (binpkgs) served to clients. |
| **multi-instance** | Portage feature: multiple binpkgs of the same package/version with different USE. |
| **transient binpkg** | binpkg built only to break a cycle (e.g. `ffmpeg[-sdl]`); discarded after the *settle-pass*, never reaches the ISO. |
| **build-pool / publish-pool** | Two views of the binhost: the build-pool includes transients (reuse across weeks); the publish-pool only finals (Assembler/users). |
| **fork-point** | Snapshot of a **stage** (`<arch>-<init>-<stage3>-<stage>.tar`), reused by every image that passes through it. |
| **toolchain-bump** | Major change in GCC/glibc/binutils (or a new snapshot pin) that triggers a full clean rebuild (`--emptytree`). |

---

## 3. Goals and Non-goals

### Goals
- **End-to-end** automation: from stage3 to the published ISO, with no manual intervention.
- Fixed **weekly releases** (Sunday 00:00), synchronized with the Gentoo autobuilds.
- ISOs **optimized per microarchitecture** (Zen 5, modern Intel) in addition to the baseline.
- **Binhost** serving packages in **USE variations** (e.g. LibreOffice Qt vs GTK).
- Auditable **input reproducibility** (same input → same set of packages/USE).
- Support for multiple **flavors** (minimal, KDE, GNOME, WM) and **inits** (systemd, openrc).
- **Third-party extensibility**: "recipe is data" — anyone adds their own arch/init/desktop in YAML.

### Non-goals (explicitly out of scope)
- It does **not** recompile the seed chain: it starts from a ready-made official stage3. See §5.1.
- It is **not** a graphical installer (Calamares/etc. is a component *of the live medium*, not of the builder).
- It does **not** pursue **bit-for-bit reproducibility** (byte-identical ISO) — only *input* reproducibility.
  Bit-for-bit on Gentoo (timestamps, build paths) would cost disproportionately; out of scope for now.
- It is **not** a standalone binary. Shidashi is a Python package for a **Linux host with systemd**
  (`systemd-nspawn`) and root: every Gentoo-specific step runs in a container — `emerge` in the image's
  stage3, the ISO tools in the toolbox (§7), the repositories from pins — so the host needs no Portage.
  `shidashi doctor` lists what the host must provide. Distributed via `pip`/`uv`, or as the
  `app-misc/shidashi` ebuild on Gentoo — never as a single binary. *(Builds have so far run on Gentoo only.)*
- It is **not** multilib by default. bentoo is **no-multilib** (pure 64-bit); 32-bit (Steam, wine, some
  drivers) is left to a future **advanced gaming support** phase, enabled **per package via
  `ABI_X86="32 64"`** in `package.use` — never through the global multilib profile. Migrating `no-multilib →
  multilib` is costly and out of scope now (see §11 and §19).

---

## 4. Design Principles

1. **Recipe is data, code is dumb.** Every variation lives in versioned YAML/Portage files; the orchestrator only executes. This is what lets third parties create their own variants without touching code.
2. **Single source of truth.** Each flavor fragment defines USE **once**, consumed both by the Factory (build) and by the Assembler (consumption) — eliminates drift.
3. **Environment purity.** Each flavor compiles in its own container; dependency closures never mix.
4. **Aggressive caching.** Phases are cacheable layers (Docker-layer style); only what changed is recompiled.
5. **Delegate to Portage.** Profiles, dependency resolution and USE are Portage's responsibility — the builder does not reimplement them.

---

## 5. Core Concepts

### 5.1 Layering on top of stage3

```
stage3 (official) ──▶ [bentoo layer: config + packages] ──▶ bentoo stage4 ──▶ ISO
```

Unlike Catalyst/Metro (which do `seed → stage1 → stage2 → stage3`), the bentoo-builder **starts from a ready-made stage3** and applies a layer. Conceptually close to **Calculate Linux** (`cl-builder`/`cl-image`), but without the coupling to the Calculate ecosystem.

**No Catalyst seed.** Building a per-microarch stage3 with Catalyst was tried and removed: the generic stage3 is only a seed, and the toolchain bootstrap and every stage recompile it with the arch's `-march` anyway, so Catalyst would rebuild what the pipeline already rebuilds.

### 5.2 The three axes of variation

```
            arch                 flavor                init
        ┌─────────┐         ┌─────────────┐       ┌──────────┐
        │ v3       │         │ minimal     │       │ systemd  │
        │ znver5   │    ×    │ kde (Qt)    │   ×   │ openrc   │
        │ arrowlake│         │ gnome (GTK) │       └──────────┘
        └─────────┘         │ wm (Wayland)│
                            └─────────────┘
```

The **desktop ≈ flavor** (KDE→Qt, GNOME→GTK, WM→Wayland/Hyprland·Sway·niri, minimal→no DE/TTY only), so the axes do not multiply naively. Composition avoids writing N×M×K complete recipes.

### 5.3 Two subsystems

```
┌─ PACKAGE FACTORY ───────────────────┐      ┌─ ISO ASSEMBLER ────────────────────┐
│ For each (arch × flavor × init):    │      │ For each (arch × init × desktop):  │
│                                     │      │                                    │
│  isolated container (systemd-nspawn)│      │  container seeds stage3            │
│   ├─ applies flavor profile + USE   │ ───▶ │   ├─ emerge --usepkgonly from the  │
│   ├─ emerge in PHASES (with ccache) │binhost│   │   right (arch,flavor) binhost  │
│   └─ produces binpkgs (multi-inst.) │      │   ├─ mksquashfs (zstd)             │
│                                     │      │   ├─ dracut (dmsquash-live)        │
│  publishes to the binhost PER ARCH  │      │   └─ grub-mkrescue/xorriso → ISO   │
└─────────────────────────────────────┘      └────────────────────────────────────┘
       (heavy compilation, slow)                  (selection + packaging, fast)
```

**Why decouple:** the Factory carries the compilation cost (hours, with ccache/sccache); the Assembler becomes nearly instantaneous (`--usepkgonly`). The weekly ISO becomes cheap.

> **Why NOT model it on the Gentoo Handbook steps:** the Handbook describes an interactive,
> human, bare-metal installation (disks, network, bootloader…), mixing *package build* and
> *image assembly* in one linear sequence. Shidashi separates those two worlds (Factory/Assembler) on
> purpose. The Handbook serves as a **coverage checklist** (no essential step forgotten),
> **not** as the execution structure — because cycles are a phenomenon of *package build order*,
> not of an *installation step* (see §6.4 and §18).

---

## 6. Package Factory (in detail)

### 6.1 Clean environments per flavor

The central requirement: **the Qt version of a package is compiled where GTK/GNOME is not even installed, and vice versa.**

- `kde/qt` container: profile + `USE="qt6 kde -gnome -gtk"` → pure Qt closure.
- `gnome/gtk` container: profile + `USE="gtk gnome -qt6 -kde"` → pure GTK closure.
- `minimal` container: system USE, **no DE / TTY only**, aggressive `-qt6 -gnome -kde -gtk`.
- `wm` container: `wayland` + Hyprland/Sway/niri (Wayland-only), lean graphical USE, **no X11**.

The purity guarantee comes from the **per-flavor container**, not from the storage feature.

### 6.2 Multi-instance binpkg

`FEATURES="buildpkg binpkg-multi-instance"` + `BINPKG_FORMAT="gpkg"` lets **N builds of the same package/version with different USE** coexist, distinguished by `BUILD_ID`.

Concrete example — **LibreOffice**:

```
app-office/libreoffice-X.Y[gtk,-qt6,-kde]   ← BUILD_ID 1  (consumed by GNOME)
app-office/libreoffice-X.Y[qt6,kde,-gtk]    ← BUILD_ID 2  (consumed by KDE)
```

When the KDE ISO Assembler runs `emerge --usepkgonly libreoffice`, the resolved USE **matches** the Qt instance → it pulls the right one. The GNOME ISO matches the GTK one. **The flavor fragment is the single source that defines that USE on both sides.**

### 6.3 Binhost partitioning

- **Per arch (mandatory):** `znver5` binpkgs (with AVX-512) **do not run** on `v3` hardware. Incompatible CFLAGS/ISA → **one binhost tree per arch**.
- **Within each arch:** multi-instance absorbs the USE variations (flavor + init).
- **No partitioning per step.** The step orders the *build*, not the *storage*; splitting per step would destroy reuse across weeks.

```
binhost/
├── v3/            (Packages index + gpkgs)
├── znver5/
└── arrowlake/
```

> **The compilation cache is shared, not segregated.** ccache (C/C++) and sccache (Rust) can use a
> single physical directory across flavors **without breaking purity**: each entry's hash **includes the flags**
> (`-march`, `CFLAGS`…), so `znver5` never collides with `v3`. Purity is guaranteed by the *container*, not
> by cache segregation. `mold` is a linker — it produces no cache; it is just a per-arch `RUSTFLAGS`/`LDFLAGS` knob.

### 6.4 Build in stages (D24)

> **Where to read and edit the process** (2026-09-28): `variants/flow.yaml` describes the
> flow — the **bootstrap** steps and the steps of **each stage** (config →
> cuts → emerge → settle → fork-point), with the emerge and settle options. The
> code in `shidashi/` executes the step *types* (`shidashi/flow.py` lists them).
> The stages and each one's sets/cuts are in the YAMLs under `variants/` (base,
> minimal, desktop, flavor), and per-package exceptions in `variants/base/quirks.yaml`.

An image is a **chain of stages**; each stage declares what comes before it
(`after:`) and becomes an `emerge` phase, with the **configuration accumulated up to it**:

```
stage     after      emerge                                   layers in effect
seed                 (verified stage3)            + rootfs/ of the layers
bootstrap seed       --oneshot of the toolchain (below)       base arch init
base      —          --emptytree @world @base                 base arch init
minimal   base       -uDN @world @extra-system  → settle      + minimal
desktop   minimal    -uDN @world @gpu …                       + desktop
<flavor>  desktop    -uDN @world @<flavor> @extra-*  → settle + flavor/<f>
```

- The **bootstrap** brings the stage3 toolchain up to the tree's versions, in order:
  locale → linux-headers + binutils → gcc → libtool → glibc →
  `@preserved-rebuild` → ccache — the steps are in `variants/flow.yaml` (executed by `shidashi/bootstrap.py`; BOOTSTRAP-PROCESS §1).
  All `--oneshot` (the world ends up empty) and with `FEATURES="-buildpkg -ccache"`
  (D22); binutils and gcc are selected **by the name** read from `/etc/env.d/`. Without
  it the base would compile `--emptytree @world` with the stage3's gcc.
- The **base** is the only complete rebuild: it "cooks" the stage3 for the
  microarchitecture and the language. The following stages use `--update --deep
  --newuse`: only what that stage's configuration changes is recompiled — the graphical
  USE comes in at `desktop`, and only what it touches is rebuilt, once for all
  four flavors.
- The config **grows along the chain**: each phase applies the layers up to its
  stage. The kde layer is not in effect while the base compiles (there is a test
  that runs the real chain and checks this).
- **Settle per shipped image** (`ships`: `minimal` and each flavor): the cycle
  cuts accumulated since the last settle are undone right there. `minimal` is
  settled halfway along kde's path, and `desktop` starts from it settled.

> **Cycle cuts (`use_break`):** some cycles require compiling with a USE
> turned off and turning it back on at the settle (§18.3). Each cut lives in the stage that creates the
> cycle; the trunk's three live in the base.

### 6.5 Fork-points per stage

```
seed ─ bootstrap ─ base ─ minimal ──┬── (minimal image)
                                    └── desktop ──┬── kde
                                                  ├── gnome
                                                  └── wm
```

Each stage writes a snapshot keyed **without the target** —
`<arch>-<init>-<stage3>-<stage>.tar` — after the settle when it is shipped. A
build resumes from the **deepest one that exists before the target**: kde built
after gnome starts from the `desktop` that gnome left behind. The target's own stage
is always rebuilt. With no stage written, the checkpoint
`<arch>-<init>-<stage3>-bootstrap.tar` spares the raw stage3 and the toolchain. (Until 2026-09-26 the key carried the flavor, and no
image reused another's trunk — F70.)

### 6.6 Build strategy: persistent trunk + wipe on toolchain

Two situations, two strategies (resolves the "cheap build" × "clean build" tension):

| Situation | Strategy |
|---|---|
| Normal week (package bumps) | **Update** (`factory --update`): restores the shipped image and runs `-uDN --changed-deps @world` with `--usepkg` over the week's `::gentoo` tree (pinned, ≥ 7 days). Fast. |
| **New generation** (new stage3 pin; toolchain or profile changed) | **Full build** in a new, empty PKGDIR (`binpkgs/<arch>/<stage3>`): bootstrap, `--emptytree` on the base, every stage. **No residue.** |

A **generation** (D26) is everything that comes out of one verified stage3 with one toolchain.
Its fingerprint (CFLAGS, CHOST, `LLVM_SLOT`, profile, gcc/binutils/glibc
versions) is recorded in the PKGDIR and checked before any emerge
— Portage compares none of this when reusing a binpkg. The update **refuses** a
plan that changes gcc, binutils or glibc: a new toolchain never slips in underneath a
built system, it opens a generation. The update ends with
`@preserved-rebuild`. (See §10 and §18.3.)

---

## 7. ISO Assembler (in detail)

| Step | Tool | Notes |
|---|---|---|
| Rootfs seed | stage3 + `emerge --usepkgonly` | Pulls everything from the binhost; does not compile |
| Compression | `mksquashfs` (zstd -19), in the toolbox | read-only rootfs |
| Live boot | **dracut** `dmsquash-live` module | overlayfs in RAM, the modern standard |
| Bootloader | `grub-mkrescue` / `xorriso`, in the toolbox | hybrid BIOS + UEFI ISO |
| Post-processing | SHA256 checksum + GPG signature | publishing |

> The squashfs and ISO tools are not the host's: they run under `systemd-nspawn` in the
> **toolbox**, the fork point of the `toolbox` stage (`base → toolbox`, `variants/toolbox/`),
> built by the factory from the same generation as the image (`shidashi/toolbox.py`). The
> ISO's GRUB is therefore pinned with everything else, and `bentoo/build.json` records the
> tools' versions.

> Note: the *installed* system may use dist-kernel + UKI (as in the reference `make.conf`), but the *live medium* uses classic dracut `dmsquash-live`.

> **The Assembler is immune to cycles (§18.6):** `--usepkgonly` installs ready-made binaries, **with no build order** — the cycle is a *build-time* phenomenon, resolved in the Factory. The Assembler only navigates the multi-instance graph and extracts its flavor's slice by the **final USE** (not "the last one compiled"). All the cycle complexity stays in the Factory.

---

## 8. Variation Matrix

### Flavors × Desktops × Inits

| Flavor | Desktops | Characteristic USE | Compatible init |
|---|---|---|---|
| `minimal` | **none (console-only)** | `-qt6 -gnome -kde -gtk` (base system) | systemd / openrc |
| `kde` | KDE Plasma | `qt6 kde wayland -gnome -gtk` | systemd / openrc |
| `gnome` | GNOME | `gtk gnome wayland -qt6 -kde` | systemd / openrc |
| `wm` | **Hyprland · Sway · niri** (Wayland-only) | `wayland -qt6 -gnome -kde` | systemd / openrc |

> **`minimal` = base stage4, no graphical environment, TTY only** (Arch/Debian-netinst model). It is the cleanest
> "control" against the KDE ISO in the pilot, and practically the fork-point trunk — **no compositor**, not even for a smoke test.
> **`wm` = Wayland-only**: **Hyprland** (default, dynamic tiling) + **Sway** (i3-compatible) + **niri**
> (*scrollable* tiling) — the most used/modern trio across distros, with no X11 dependency at all
> (consistent with the `wayland` + `seatd`/`elogind` base). A showcase, separate from minimal because it carries more dependencies.

### init ↔ seat coupling (the non-orthogonal part)

| | systemd | openrc |
|---|---|---|
| logind | `systemd` (native) | **`elogind`** |
| seat (WM/Wayland) | systemd-logind | elogind + `seatd` |
| global USE | `systemd -elogind` | `elogind -systemd` |

The **init** fragment carries the seat USE; the **desktop** fragments assume "seat already resolved" and stay init-agnostic.

---

## 9. Per-Architecture Optimization

### 9.1 Target table

| Target | `-march` | `GOAMD64` | `RUSTFLAGS target-cpu` | AVX-512 |
|---|---|---|---|---|
| **baseline** | `x86-64-v3` | `v3` | `x86-64-v3` | no |
| **Zen 5** | `znver5` | `v4` | `znver5` | **yes** |
| **Arrow Lake** | `arrowlake` | `v3` | `arrowlake` | **no** |

### 9.2 ⚠️ The `x86-64-v4` trap for Intel

`x86-64-v4` **requires** AVX-512, but Intel **client** (Alder Lake onward) **removed** AVX-512. A `-march=x86-64-v4` ISO **does not run** on Arrow Lake. For "modern Intel", use the specific `-march` (`arrowlake`), **never v4**. `v4` only serves Intel **server** (Granite Rapids) or Zen 4/5.

### 9.3 Knobs that must move TOGETHER

The `arch` fragment must parametrize **all** CPU controls together, otherwise a v3 Go/Rust leaks into a znver5 binpkg:
- `COMMON_FLAGS` (CFLAGS/CXXFLAGS/…)
- `GOAMD64`
- `RUSTFLAGS -C target-cpu`
- `CPU_FLAGS_X86` — **set manually per target** (do not use `cpuid2cpuflags`, which detects the host)
- `CHOST` (when applicable)

### 9.4 Current build host and validation tiers

Host: **AMD Ryzen 9 9950X (Zen 5, v4 class, with AVX-512).** Consequences:
- **Builds** any target (GCC 16 cross-compiles znver5/arrowlake without trouble).
- **Runs natively** `v3` **and `znver5`** binpkgs (Zen 5 has AVX-512) → both are boot-testable on the host.
- **Does not run** `arrowlake`: `-march=arrowlake` may use Intel-specific ISA absent on AMD → SIGILL.

**Test strategy per tier:**

| Tier | Archs | Validation |
|---|---|---|
| Tier 1 | `v3`, **`znver5`** | Smoke test + native boot on the host (9950X) |
| Tier 2 | `arrowlake` | **Boot test via QEMU (TCG)** — build-only on the AMD host; no real Intel hardware |

> Change vs. the previous host (Zen 3): **`znver5` moved up to Tier 1** — it is no longer build-only and becomes
> natively verifiable, because the 9950X has AVX-512.
>
> **`arrowlake` validates only through QEMU (TCG):** since Arrow Lake **has no AVX-512**, its ISA fits within TCG
> emulation — a faithful enough boot test without acquiring Intel hardware.

---

## 10. Reproducibility

- **Level pursued: *input* reproducibility** (same input → same set of packages/USE), **not** bit-for-bit. The goal is to **replicate the process without breaking errors**, not to produce a byte-identical ISO.
- **`::gentoo` snapshot pin** per release (dated squashfs) → deterministic input.
- **`-march=native` is banned** (§9.3) — a prerequisite for any determinism; always an explicit `-march` per arch.
- Known tension: the system uses `ACCEPT_KEYWORDS="~amd64"` (testing, changes fast). The snapshot pin is **even more critical** in that context — it is not optional.
- Each release records: stage3 hash, repo snapshot hash, recipe hash, toolchain versions.
- Versioned output: `bentoo-<flavor>-<init>-<arch>-<date>.iso` + `.sha256` + `.asc`.

---

## 11. Weekly Release Flow

Fixed cadence: **every Sunday at 00:00.**

**Seed per init (no-multilib).** Shidashi seeds each variant from the corresponding init's
**no-multilib** stage3 — **not** from the `desktop` tarball (which is multilib).
Switching init is not done by profile conversion (a "difficult" operation according to the Handbook):
each init starts from its own stage3.

| init | stage3 seed (official autobuild) | anchor base profile |
|---|---|---|
| systemd | `stage3-amd64-nomultilib-systemd-<snapshot>.tar.xz` | `default/linux/amd64/23.0/no-multilib/systemd` |
| openrc  | `stage3-amd64-nomultilib-openrc-<snapshot>.tar.xz`  | `default/linux/amd64/23.0/no-multilib` |

> Pilot snapshot: **`20260517T170110Z`** (both inits from the same snapshot, for reproducibility).

```
┌─ trigger (cron: Sunday 00:00) ────────────────────────┐
│ CI reads the autobuild pointer files (no-multilib):   │
│   .../latest-stage3-amd64-nomultilib-systemd.txt      │
│   .../latest-stage3-amd64-nomultilib-openrc.txt       │
│ Compares each one with the last recorded build.       │
└──────────────────┬────────────────────────────────────┘
                   │ changed? (toolchain-bump? → wipe; else → delta)
                   ▼
        ┌─ Factory (arch × flavor × init matrix) ──┐
        │ rebuild of the changed binpkgs           │
        └──────────────────┬───────────────────────┘
                           ▼
        ┌─ Assembler (arch × init × desktop matrix) ─┐
        │ re-spin of the ISOs from the binhost        │
        └──────────────────┬──────────────────────────┘
                           ▼
        ┌─ Publish: checksum + GPG + upload ──────────┐
        └─────────────────────────────────────────────┘
```

CI as a matrix (conceptual example):

```yaml
on:
  schedule:
    - cron: "0 0 * * 0"   # Sunday 00:00 UTC
strategy:
  matrix:
    arch:    [v3, znver5, arrowlake]
    init:    [systemd, openrc]
    desktop: [minimal, kde, gnome, wm]
```

---

## 12. Technology Stack

### Language: **Python ≥ 3.14** (final)

| Argument | Detail |
|---|---|
| **Gentoo's tooling is Python** | Portage, its `Packages` index and its build logs are Python-shaped; Shidashi drives `emerge` **inside the containers** and parses its output, sharing the language of the tools it orchestrates. It never imports Portage on the host. |
| **The host needs no Portage** | Portage lives in the containers (the image's stage3, the toolbox). The host is any Linux with systemd that passes `shidashi doctor`. Distribute = **`pip`/`uv`**, or the **`app-misc/shidashi` ebuild** on Gentoo. |
| **Subprocess-bound glue** | The heavy work is `emerge`'s; language performance is irrelevant. **Iteration speed** dominates (recipes change every week). |
| **Recoverable rigor** | `pydantic` (recipe schema) + `mypy --strict` + `ruff` cover type safety where errors hurt. |
| **Grows without a rewrite** | A dashboard/binhost server fits in FastAPI + asyncio; the core stays. |

> **Rust** was ruled out: it optimizes the correctness of the layer where the bugs are *not* (USE flag/`emerge`/shell), at the highest iteration cost. **Go** would only win for a remote orchestrator that did not touch local Portage — and that is the *dashboard* (FastAPI already covers it).

**Modern Python 3.14 features to exploit:**
- **PEP 695** — new generics and type alias syntax (`type Recipe = ...`, `def merge[T](...)`) in the recipe models.
- **PEP 749** — *lazy* annotations by default → lower import cost, great for pydantic.
- **PEP 750 — t-strings** — **injection-safe** assembly of `emerge`/shell commands in `container.py`.
- **`match`** — dispatch of the axis deep-merge and of parsing the `pretend-resolve` results.
- **`tomllib`** (stdlib) — TOML reading without an extra dependency.

### Components

| Role | Choice |
|---|---|
| Orchestrator | Python 3.14 + pydantic + mypy + ruff |
| Helpers in the container | Bash (`emerge`, `eselect`) |
| Isolation | `systemd-nspawn` |
| Phase cache | btrfs subvol / tarball |
| Compilation cache | ccache (C/C++) + sccache (Rust) — **shared** across flavors |
| Linker | mold (per-arch knob) |
| squashfs compression | zstd |
| Live boot | dracut `dmsquash-live` |
| ISO | `grub-mkrescue` / `xorriso` |
| Binpkg | gpkg + multi-instance + GPG signature |
| Shidashi distribution | Python package (`pip`/`uv`); `app-misc/shidashi` ebuild (overlay) on Gentoo |
| CI | GitHub Actions (large runner) or self-hosted |

### Verified environment (current host)
`GCC 16.1.0` · `Clang 22.1.6` · `mold 2.41` · `Portage 3.0.79` · `Python 3.14.5` · `Go 1.26.3` · `Rust 1.95.0` · profile `no-multilib/systemd` · CPU **Ryzen 9 9950X (Zen 5)**. GCC supports every target `-march`.

---

## 13. Repository Layout

Co-located **per axis** (`variants/<axis>/<name>/`): a variant's configuration in a single folder — makes it easy to add/remove a whole variant (and third-party contributions). The **content** (the sets) lives in a single library, `variants/kits/` (D25): the layers only configure and choose.

```
stages/                          # project root (this repo)
├── OVERVIEW.md                  # this document
├── README.md · pyproject.toml · .gitignore
├── shidashi/                        # Python package (orchestrator)
│   ├── cli.py                   # Typer CLI (subcommands)
│   ├── config.py                # paths
│   ├── recipe.py                # pydantic models + axis merge
│   ├── container.py             # systemd-nspawn wrapper
│   ├── factory.py               # Package Factory subsystem
│   ├── assembler.py             # ISO Assembler subsystem
│   ├── phases.py                # phase execution + layer cache
│   ├── binhost.py               # multi-instance management + index + signing
│   ├── image.py                 # squashfs + dracut + ISO
│   └── toolbox.py               # the Bentoo rootfs the ISO tools run in
├── variants/                    # composable axes (recipe + portage) + the set library
│   ├── kits/                    # ALL the sets, by category (D25) — unique names
│   │   ├── core/                #   base (aggregator) boot fs portage shell hardware admin archive network
│   │   ├── system/              #   extra-system (aggregator) net-tools monitoring laptop firmware misc …
│   │   ├── graphics/ services/ internet/ media/ dev/ virt/
│   │   ├── groups/              #   extra-desktop extra-dev extra-media extra-virt
│   │   └── desktops/            #   kde gnome wm
│   ├── base/                    # STAGE 1 — the core; the only complete rebuild (D24)
│   │   ├── recipe.yaml
│   │   └── portage/             # base /etc/portage (CORE/FEATURES/DISTDIR/PKGDIR…)
│   ├── minimal/recipe.yaml      # STAGE 2 — after: base; shipped image (console)
│   ├── desktop/recipe.yaml      # STAGE 3 — after: minimal; graphical infra, no apps
│   ├── arch/
│   │   ├── v3/{recipe.yaml, portage/}
│   │   ├── znver5/{recipe.yaml, portage/}
│   │   └── arrowlake/{recipe.yaml, portage/}
│   ├── flavor/
│   │   ├── kde/{recipe.yaml, portage/}      # STAGE 4 — after: desktop; shipped image
│   │   ├── gnome/{recipe.yaml, portage/}
│   │   └── wm/{recipe.yaml, portage/}     # Wayland-only: Hyprland, Sway, niri
│   └── init/
│       ├── systemd/{recipe.yaml, portage/}
│       └── openrc/{recipe.yaml, portage/}
├── seeds/stage3.toml            # pinned stage3 pointer (§10/§11)
├── scripts/
│   └── postinstall.d/           # idempotent customizations
└── .github/workflows/release.yml

# bentoo overlay: external to this repo, at /var/db/repos/bentoo
```

**Set → axis mapping** (the sets that the §6.4 phases consume):

Sets come in **two levels**: leaves with atoms, and aggregators that reference
other leaves by `@name` (Portage expands them recursively). A flavor declares
two or three aggregators instead of twenty leaves.

Every set lives in `variants/kits/<category>/<name>`. The category is only for
people: Portage sees sets in a flat namespace
(`/etc/portage/sets/<name>`), so a name is **unique** across the whole library —
a test enforces it. **Where the file lives does not decide who installs it**: only
what a recipe declares (`sets:`) is installed, with the `@refs` followed until
closure. Per-flavor tuning is explicit: declare a set, or `exclude:` atoms
from it. There is no same-name override between layers. A set that only applies under
one init goes in the stage's `init_sets: {<init>: [...]}` — kde's display manager:
`kde-dm-plasma` (plasma-login-manager, requires systemd) or `kde-dm-sddm`.

| Set | Location | Scope |
|---|---|---|
| `base` (aggregator) | `kits/core/` | **universal** — declared by `base/recipe.yaml`, goes into every image |
| `extra-system` | `kits/system/` | the console kits — declared by every flavor |
| `extra-desktop`, `extra-media`, `extra-dev`, `extra-virt` | `kits/groups/` | **optional** — each flavor declares the ones it wants |
| leaves (`boot`, `fs`, `audio`, `web`, `devel` …) | `kits/<category>/` | referenced by the aggregators; a flavor may declare one directly (e.g. `gpu`) |
| `kde`, `gnome`, `wm` | `kits/desktops/` | desktop-**specific** — each declared only by its own flavor |

Which phase installs which sets is **declared** in `base/recipe.yaml` (the `sets` field of each
phase), not inferred from the phase name. `phase_target` intersects with the recipe's
sets, so listing there a set that only some flavors declare is safe.

`minimal` has no desktop set — it consumes `@base` and `@extra-system`. Each fragment (`recipe.yaml`,
`portage/`, `sets/`) is resolved by `shidashi recipe` via deep-merge in the order `base → arch → flavor → init`.

---

## 14. Recipe Schema (example)

An image is the chain of stages up to the target, plus the `arch` and `init` axes
(`config.load_recipe(arch, target, init)`). Each stage chooses **sets** and
**cuts**; the **USE** lives only in each layer's `portage/make.conf` — it is the
file the build reads, and the only source:

```yaml
# variants/flavor/kde/recipe.yaml
stage: kde
after: desktop
ships: true                     # shipped image: settle + settled fork-point
sets: [kde, extra-desktop, extra-media, extra-dev, extra-virt]
```

```yaml
# variants/minimal/recipe.yaml
stage: minimal
after: base
ships: true
sets: [extra-system]
```

```yaml
# variants/init/openrc/recipe.yaml
init: openrc
profile_suffix: ""                    # profile without /systemd
phases_prepend:
  - { name: seat, packages: [sys-auth/elogind, sys-auth/seatd] }
```

> **There is no longer a `use_prefer` or an `override_ok`** (F69). `use_prefer` was only
> displayed by `recipe show` and never reached a build; gnome, xfce, wm and openrc
> depended on it and therefore never had their characteristic USE applied — it
> moves to their `make.conf` in step 3 of D24. Without `use_prefer` there is no USE
> conflict between layers for `override_ok` to arbitrate.

**Profile resolution (decision: `no-multilib` only, everything above via USE).** The only axis that touches the
profile is **init**. *All* flavors inherit the same anchor; **there is no DE profile**:

    default/linux/amd64/23.0/no-multilib[/<init.profile_suffix>]

- `base` pins `default/linux/amd64/23.0/no-multilib` (no-multilib is the default — §3).
- `init.profile_suffix` appends `/systemd` (systemd) or nothing (openrc).
- **The flavor does NOT contribute a profile.** The "desktop layer" (KDE/GNOME/WM) is built
  entirely **on top of** no-multilib via `portage/` (make.conf + package.use) + `sets` (§13).

Resolved profile (identical for minimal/kde/gnome/wm — it only changes per init):
- `* + openrc`  → `default/linux/amd64/23.0/no-multilib`
- `* + systemd` → `default/linux/amd64/23.0/no-multilib/systemd`

> ⚠️ **Why there is no desktop profile.** In the official tree, `desktop/*` and `no-multilib` are **siblings**
> under `default/linux/amd64/23.0/` — there is no `no-multilib/desktop/plasma`, so they do not compose. Instead of
> creating custom profiles in the overlay, bentoo stays **on the `no-multilib[/systemd]` profile only** and encodes
> all DE differentiation via `portage/package.use` + sets (which are already load-bearing, §18.2). Advantage:
> no custom profiles for third parties to maintain; the layers (`portage/` + sets) are the **only** source of the
> graphical layer. (Decision §19.1.)

---

## 15. make.conf Decomposition

The reference `make.conf` (already organized into named groups) maps directly:

| Group in make.conf | Destination fragment |
|---|---|
| `CORE KERNEL COMPRESSOR GRAPHICS DEVELOPMENT PERFORMANCE FILESYSTEM IMAGE AUDIO VIDEO NETWORK DEVICES SECURITY VIRTUALIZATION` | `variants/base/portage/` |
| `COMMON_FLAGS GOAMD64 RUSTFLAGS CPU_FLAGS_X86 CHOST` | `variants/arch/<x>/portage/` |
| `DESKTOPS REMOVED` (graphical part) | `variants/flavor/<y>/portage/` |
| `SYSTEMD` / elogind | `variants/init/<z>/portage/` |
| `FEATURES DISTDIR PKGDIR ccache/sccache` | `variants/base/portage/` |

> The first bentoo release is, essentially, the current make.conf factored into
> `base + arch/v3 + flavor/kde + init/systemd` (and `flavor/minimal`). **It is not created from scratch —
> the existing one is factored** (with less USE than the example).

---

## 16. Binhost: Security and Distribution

- **Signing mandatory if public:** gpkg supports native GPG signing. Enable `binpkg-request-signature` on the clients. A public binhost without signatures is a supply-chain vector.
- **Per-arch index:** each `binhost/<arch>/Packages` is independent.
- **Evolving hosting (local → online):** development uses a **local/self-hosted** binhost on the host itself (zero cost); the public phase moves to **Cloudflare R2** (S3-compatible object storage, **no egress fee**) for the binhost and ISOs.
- **Bonus:** the same binhost that feeds the Assembler can serve bentoo's **end users** (Redcore/Sisyphus model), speeding up installations.

---

## 17. Development Roadmap

> Legend: `[x]` implemented and validated by the off-host suite · `[ ]` pending.
> *Build/boot pilots on a root Gentoo host are host-gated and remain deferred
> even where the code is complete (stories 003/004); noted inline.*

### Phase 0 — Foundation (MVP)
- [x] Python skeleton (≥3.14) + pydantic + per-axis recipe structure.
- [x] `recipe.py` + `cli.py`: `recipe show/validate` (axis deep-merge) — the first real deliverable.
- [x] `shidashi pretend <arch> <flavor> <init>` (cycle discovery, costs seconds).
- [x] `systemd-nspawn` wrapper.
- [x] stage3 detector (pointer file).
- [x] Minimal pipeline: **`v3 × minimal × systemd`** → stage4 tarball, then `v3 × kde × systemd`. *(`shidashi factory` complete + tested; root-host build pilot deferred — stories 003/004.)*

### Phase 1 — ISO
- [x] Assembler: squashfs + dracut `dmsquash-live` + hybrid ISO. *(impl. + off-host tests; image path validated by a real boot — assembly pilot via the Phase 2 binhost still deferred.)*
- [x] Automated boot smoke test (QEMU + native on the 9950X). *(`scripts/smoke-iso.sh` + host-gated `tests/test_smoke_iso.py`; **boot validated on QEMU/KVM on the 9950X** — grub → dmsquash-live mounts the squashfs → systemd switch-root → userspace.)*

### Phase 2 — Binhost & Factory
- [x] Factory with phases + fork-point cache. *(impl. + tests; root-host pilot deferred.)*
- [x] Persistent-trunk strategy (fork-point reuse) — weekly delta over the binhost. *(impl. in `factory.py`; host pilot deferred.)*
- [ ] Wipe-on-toolchain + toolchain-bump phase (§6.6): GCC/glibc/binutils bump detection → `--emptytree` + `@preserved-rebuild` + subslot-rebuilds. *(story 006; zero occurrences in the code today.)*
- [ ] Multi-instance binhost + signing.
- [ ] LibreOffice Qt vs GTK as a proof of concept. *(Qt variant present in `flavor/kde`; GTK counterpart pending.)*

### Phase 3 — Matrix
- [x] arch axis: `znver5` (tier 1), `arrowlake` (tier 2, build-only, QEMU/TCG boot test). *(v3/znver5/arrowlake recipes complete; `arrowlake` QEMU/TCG boot test deferred.)*
- [ ] flavor axis: `gnome`, `wm` (Wayland-only: Hyprland, Sway, niri). *(`kde` and `minimal` curated; `gnome` ships `gnome-light` + GDM (unreleased); `wm` is still a placeholder — an empty set, its ISO boots to a tty.)*
- [x] init axis: `openrc`. *(systemd + openrc complete.)*

### Phase 4 — Automation
- [ ] Weekly CI matrix (cron Sunday 00:00) + publishing + checksums/GPG. *(scaffold gated with `if: false`; still missing a self-hosted runner (Linux with systemd, root — or the builder VM below) + pointer file reading + a `release` command.)*
- [x] Reproducible snapshot pin. *(stage3 in `seeds/stage3.toml`; `::gentoo` in `seeds/gentoo.toml` — signed daily snapshot, at least 7 days old (cooldown, D26), the overlays by commit in `seeds/overlays.toml`; factory/assemble/pretend bind only these pins, never the host's `/var/db/repos`.)*

- [x] Builder VM: builds as the guest's root, no sudo on the host. *(`lab/vm/builder.sh`: a Bentoo `minimal` ISO under `shidashi vm`, the host's cache read-only under an overlay on the VM's disk; built the minimal, gnome and wm ISOs on 2026-10-02.)*
- [ ] Builder image (OCI) under Kata Containers. Shidashi and its dependencies in a Bentoo image, run per job with `podman run --runtime kata`: each build in a disposable micro-VM with its own kernel, no sudo on the host, on any distribution with Podman and Kata — the packaged form of the builder VM, and an isolated self-hosted CI runner (each job in its own micro-VM). First experiment: the `minimal` rootfs as an OCI image under the kata runtime, checking that `systemd-nspawn` runs inside. Open points: the Kata guest kernel needs overlayfs, namespaces and device support (`sys-kernel/kata-guest-kernel` is in the overlay); Kata shares the rootfs over virtiofs, measured ~25% slower for binpkg installs in the builder VM — a block volume for the build tree; the micro-VM's memory is fixed (2 GB by default) and must be sized for builds.

### Phase 5 — Operations (optional)
- [ ] Release dashboard (FastAPI).
- [ ] Public binhost for end users (Cloudflare R2).
- [x] `app-misc/shidashi` ebuild in the overlay. *(0.1.1, 2026-10-03: no `env.d`, the recipes in `/usr/share/shidashi`.)*

---

## 18. Empirical Validation and Refinements (resolution test)

> Records what a **resolution test** (`emerge --pretend`, **without compiling**) revealed
> about the real pipeline, and the architecture refinements that followed from it.
> Tool: `shidashi pretend <arch> <flavor> <init>` — pipeline `seed → apply_portage → run`.

### 18.1 Methodology

Before spending hours compiling, the tree is resolved with `emerge --pretend --emptytree`
inside a seeded stage3, measuring **the package list** and detecting **circular
dependencies** — without a build. Scenario **T0** = reference make.conf with the full USE at
once ("all at once", naive style). Cost ~seconds; zero risk. Pilot target:
`v3 × kde × systemd`, profile `no-multilib/systemd`, stage3 `nomultilib-systemd`.

### 18.2 T0 findings

1. **make.conf alone does NOT resolve.** make.conf alone (without `package.use`) on a clean stage3
   makes the resolver demand a batch of USE changes (`systemd policykit`,
   `qt5compat qml`, `kconfig qml`, `qtbase libproxy`…). **The curated `package.use` is
   load-bearing**, not cosmetic — reinforces §4.2 (single source of USE).
2. **The overlay is part of the resolution.** Without the bentoo overlay (`/var/db/repos/bentoo`),
   leaf packages lagging in the Python migration (e.g. `libffado` × `python3.14`) hit
   `REQUIRED_USE` that the overlay already fixes. → Factory **and** Assembler must configure
   **all** of the target's repos (gentoo + bentoo + …), not just `::gentoo`.
3. **The circular dependency is real and concrete:**
   ```
   libsdl2 ─▶ pipewire ─▶ ffmpeg ─▶ libsdl2        (build-time)
   break:   ffmpeg -sdl  |  libsdl2 -pipewire  |  pipewire -ffmpeg
   ```
   Confirms: the full USE at once **gets stuck in a cycle**; the resolver **lists** the breaks
   but **does not apply them on its own** — the Factory must encode them.
4. **The first barrier is not the cycle — it is USE curation.** Resolution died on USE
   before reaching the cycle; only with `package.use` + overlay did the cycle show up.

### 18.3 Staged USE discipline

- ***Completeness* USE (qt6/gtk/kde/gnome/VIDEO_CARDS/L10N): final and global from step 1.**
  They cause no cycle and do not bloat `@system` (which does not reference them). Staging them is what creates
  **spurious duplication** of deps — avoid it.
- ***Cycle-breaking* USE (`use_break`, e.g. `ffmpeg -sdl`): transient, per step,
  scoped via `package.use`.** It is the only one that is staged: *break-pass* (USE off) →
  *settle-pass* (USE on, with the cycle partner already present). The same package compiles twice
  **on the first build** — a cost paid once, amortized in the binhost. **Curated manually.**
- `--newuse` does **not** recompile on a `-march`/CFLAGS change → the trunk's first build
  uses `--emptytree`; following weeks are the **delta** over the persistent binhost. A
  **toolchain-bump** forces a new `--emptytree` (§6.6).

### 18.4 Transient binpkg — a new category

The break-pass `ffmpeg[-sdl]` is a **transient binpkg**: it exists only to break the cycle,
is replaced by the final `ffmpeg[sdl]`, and **must never reach the ISO/user**. Three
multiplicities coexist in the multi-instance pool — and the GC must tell them apart:

| Type | Example | GC policy |
|---|---|---|
| Flavor variant (legitimate) | `poppler[qt6]` vs `poppler[gtk]` | keep **all** (each goes to its own ISO) |
| Old version | `poppler-24` vs `poppler-25` | keep the N most recent |
| Bootstrap transient | `ffmpeg[-sdl]` | **prune** after settle / build-pool only |

→ `binhost.gc()` is **USE-aware** (which instance is the final one for each flavor), **not**
"global keep-N" (otherwise it prunes a flavor variant thinking it is a duplicate).

### 18.5 The binhost is a graph, not an array

The per-arch binhost is a set of `(CPV, USE)` nodes — not "the last one compiled". Each
flavor is a **consistent slice** of that graph. Divergence between flavors comes from (a) the
flavor's global USE touching dozens of packages **directly** and (b) **USE-dep
propagation** (`kio[qt6]` forces `qtbase[qml]`…). Packages without DE USE (toolchain, base libs)
are **identical** across flavors → one shared instance (the fork-point trunk).

**Two pool levels:**
- **build-pool** — includes transients; reused across weeks so cycles are not re-broken.
- **publish-pool** — finals only; consumed by the Assembler and users (filtered `Packages`
  index). This is what recovers the idea of a "layered binhost" (trunk vs flavor).

### 18.6 The Assembler is immune to cycles

A cycle is a *build-time* phenomenon. `emerge --usepkgonly` **installs ready-made binaries**, with no
compilation order → the cycle does not exist at assemble time. The Assembler resolves the **final USE** and
the multi-instance match picks the right instance per flavor (`poppler[qt6]` for KDE, `[gtk]` for
GNOME). **All the cycle complexity stays in the Factory; the Assembler stays trivial.**
Requirement: the final USE of the Factory's *settle-pass* **==** the USE the Assembler resolves
(otherwise `--usepkgonly` fails with no match). An automated test comparing the USE
promised by the fragment with the one recorded in the `BUILD_ID` is worthwhile.

### 18.7 `shidashi pretend` as a cycle discoverer

The `shidashi pretend <arch> <flavor> <init>` command is promoted to a **curation instrument**:
it runs `emerge --pretend` inside the container, collects the suggested breaks ("break this cycle
by changing USE X") and **manually feeds** the `use_break` of the recipe steps. It stops being
just a test and becomes part of the curation pipeline.

---

## 19. Decisions

### 19.1 Taken (recorded)

| Topic | Decision |
|---|---|
| **Pipeline structure** | Emerge phases (Factory) + Assembler. Handbook = checklist, not structure (§5.3). |
| **Re-seed vs. trunk** | **Hybrid:** persistent trunk for the weekly delta; **full `--emptytree` wipe** on toolchain-bump (§6.6). |
| **`use_break`** | **Manual** curation per flavor, fed by `pretend-resolve` (§18.7). |
| **Determinism** | Of **input/configuration** (replicate without errors), **not** bit-for-bit (§10). |
| **Shidashi distribution** | Python package on a Linux host with systemd (`shidashi doctor`); `app-misc/shidashi` ebuild on Gentoo; never a single binary (§3, §12). |
| **`minimal`** | **Console-only, TTY-only** flavor — no compositor, not even for a smoke test (§8). |
| **`wm`** | **Wayland-only:** Hyprland (default) + Sway + niri — no X11 dependencies (§8). |
| **Layout** | Co-located **per axis** (`variants/<axis>/<name>/`); external overlay (§13). |
| **Build host** | **Ryzen 9 9950X (Zen 5)** → `v3` and `znver5` in Tier 1; `arrowlake` Tier 2 (§9.4). |
| **`arrowlake` validation** | **QEMU (TCG)** — no AVX-512, the ISA fits within emulation; no real Intel hardware (§9.4). |
| **Cadence** | Fixed release **every Sunday 00:00** (§11). |
| **multilib** | **no-multilib** by default; 32-bit only in the future gaming phase, **per package via `ABI_X86="32 64"`** (§3). |
| **Seed** | **no-multilib** stage3 per init (systemd/openrc), from the same pinned snapshot (§11). |
| **`ships`** | The stage is a shipped image (`minimal` and each flavor): it gets a settle and a settled fork-point (§6.4). |
| **Desktop layer** | **`no-multilib[/systemd]` profile only** — no DE profiles in the overlay; KDE/GNOME/WM built **on top** via `package.use` + sets (§14). |
| **Language** | **Python ≥ 3.14** (on its way to becoming Gentoo's default), modern features (PEP 695/749/750, `match`) (§12). |
| **Hosting** | **Local now → Cloudflare R2 later** (no egress) for the binhost and ISOs (§16). |
| **Toolchain-bump** | Explicit phase that triggers `@preserved-rebuild` + subslot-rebuilds (§6.6). |

### 19.2 Still open

**No open architecture items.** All earlier decisions were closed and moved
to §19.1: the `wm` WMs (Wayland-only: Hyprland/Sway/niri), `arrowlake` validation (QEMU/TCG),
`minimal` (TTY only), hosting (Cloudflare R2), multilib for the gaming phase (per package via
`ABI_X86="32 64"`) and the desktop layer (**`no-multilib[/systemd]` profile only**, DE via
`package.use` + sets).

Next decisions arise during implementation (Phase 0): exact package names/versions per set,
binhost GC policy and the snapshot pin format.

---

## 20. References

- [Catalyst — Gentoo Wiki](https://wiki.gentoo.org/wiki/Catalyst)
- [Funtoo Metro](https://github.com/funtoo/metro) · [stage4.spec](https://github.com/funtoo/metro/blob/master/targets/gentoo/stage4.spec)
- [Calculate Linux — Interactive system build](https://old.calculate-linux.org/main/en/interactive_system_build)
- [Distributions based on Gentoo — Gentoo Wiki](https://wiki.gentoo.org/wiki/Distributions_based_on_Gentoo)
- [Handbook:AMD64 — Gentoo Wiki](https://wiki.gentoo.org/wiki/Handbook:AMD64) (used as a coverage checklist)
- dracut `dmsquash-live`, `app-cdr/livecd-tools`, `binpkg-multi-instance` (Portage docs)
