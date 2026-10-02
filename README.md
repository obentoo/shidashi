<div align="center">

# Shidashi 仕出し

**The catering of [Bentoo](https://github.com/obentoo/bentoo):** builds Bentoo (Gentoo stage5)
images and live ISOs from an official Gentoo stage3, phase by phase.

[![CI](https://github.com/obentoo/shidashi/actions/workflows/release.yml/badge.svg)](https://github.com/obentoo/shidashi/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-blue.svg)

</div>

---

## What it does

Shidashi starts from an **official, verified stage3** and grows it, stage by stage, into the
Bentoo images. Every stage is built in an isolated `systemd-nspawn` container, every package it
compiles lands in a **binhost** as a binpkg, and the live ISOs are assembled from that binhost
without compiling anything.

```text
stage3 ─► bootstrap ─► base ─► minimal ─► desktop ─┬─► kde
         (toolchain)                               ├─► gnome
                                                   └─► wm
           each stage leaves a fork point; the binpkgs go to the binhost

binhost ─► assemble ─► live ISO (BIOS + UEFI) ─► vm test
```

The full architecture is in **[OVERVIEW.md](OVERVIEW.md)**.

## Status

| Area | State |
|---|---|
| Package Factory (stage3 → binpkgs) | ✅ implemented and tested |
| ISO Assembler (binpkgs → live ISO) | ✅ implemented and tested |
| Boot test (`shidashi vm test`, BIOS + UEFI) | ✅ the `kde` and `minimal` ISOs pass |
| Weekly releases (every Sunday, 00:00 UTC) | 🚧 planned: needs a self-hosted Gentoo runner |

652 tests; lint, types and tests run on every push. The roadmap is in
[OVERVIEW.md §17](OVERVIEW.md#17-development-roadmap).

## Model

### Axes

Every image is one combination of three axes, each defined under [`variants/`](variants/):

| Axis | Values |
|---|---|
| **Flavor** | `minimal` (console only) · `kde` (Qt) · `gnome` (GTK) · `wm` (Wayland-only: Hyprland, Sway, niri) |
| **Arch** | `v3` (x86-64-v3, baseline) · `znver5` (Zen 5, tier 1) · `arrowlake` (tier 2, build-only) |
| **Init** | `systemd` · `openrc` |

### Two subsystems

| Subsystem | Command | In | Out |
|---|---|---|---|
| **Package Factory** | `shidashi factory` | a verified stage3 | binpkgs in the binhost, a fork point per stage |
| **ISO Assembler** | `shidashi assemble` | the binhost | a live ISO, installed only from binpkgs |

### Stages and fork points

An image is a chain of stages: `base → minimal → desktop → <flavor>`. Each stage installs its
own kits on top of the previous one and leaves a **fork point**, a snapshot the next builds
resume from. The trunk (`base`, `minimal`, `desktop`) is built once per arch and init and is
shared by every flavor.

### Toolbox

The ISO is made with Bentoo's own tools, not the host's. The `toolbox` stage
([`variants/toolbox/`](variants/toolbox/recipe.yaml)) branches off the base with GRUB (BIOS +
UEFI), squashfs-tools, xorriso and mtools; `shidashi assemble` runs `mksquashfs`,
`grub-mkrescue` and `unsquashfs` in its fork point, under `systemd-nspawn`. The ISO's
bootloader therefore comes from the same generation as the image, and its versions are
recorded in the medium's `bentoo/build.json`. `shidashi build` builds the toolbox first; on
its own, it is `shidashi factory <arch> toolbox <init>`.

### Kits

[`variants/kits/`](variants/kits/README) is the library of **every package Bentoo builds**, by
category. A stage declares which kits its image installs; a stage's `exclude:` and `include:`
adjust that per image. A kit line marked `#atom` is built for the binhost but installed by no
image, so the binhost can offer more than the ISOs ship.

### Reproducible inputs

- The `::gentoo` snapshot and the overlays are **pinned** per release: the snapshot by date,
  and only once it is at least 7 days old; the overlays by commit.
- A **generation** is everything built from one stage3 with one toolchain. Its fingerprint is
  recorded in the binhost; if the toolchain changes, the build is refused instead of mixing
  binpkgs from two toolchains, and a new generation starts from an empty binhost.
- Every run is **audited**: its steps, the packages built or reused, and the result.

### Why Python

Portage *is* a Python library, so Shidashi speaks its language: recipes are validated with
pydantic, and the build steps run `emerge` inside the containers.

## Requirements

- **To develop and run the tests:** Python ≥ 3.14 and [uv](https://docs.astral.sh/uv/). Any
  Linux works.
- **To build images:** a **Gentoo host**, with root, plus `systemd-nspawn`. The ISO tools
  (`mksquashfs`, `grub-mkrescue`, `xorriso`) are not the host's: they run in the
  **toolbox**, a Bentoo stage built by the factory (see below). `shidashi vm` needs QEMU/KVM
  and `xorriso`.

Portage itself runs inside the build containers; the host's Python does not need it.

## Getting started

```sh
git clone https://github.com/obentoo/shidashi.git
cd shidashi
uv sync --frozen --extra dev     # exactly uv.lock, with the dev tools
uv run shidashi --help
```

Run the same checks as CI:

```sh
uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest
```

## Usage

```sh
shidashi recipe list                       # the arches, images and inits available
shidashi recipe show v3 kde systemd        # the resolved recipe of one image
shidashi world kde systemd                 # its packages, kit by kit (nothing is written)

sudo shidashi factory v3 toolbox systemd   # the toolbox the ISOs are made in
sudo shidashi factory v3 kde systemd       # build the binpkgs, stage by stage
sudo shidashi assemble v3 kde systemd      # assemble the live ISO from the binhost
shidashi vm test bentoo-…-kde-systemd-v3.iso   # boot it and check what it declares
```

| Command | What it does | Root |
|---|---|:---:|
| `recipe list` / `show` / `validate` | inspect and validate the resolved recipes | |
| `world` | write, check or print each image's package list | |
| `kits check` | check every kit atom against the pinned trees | |
| `pretend` | resolve a recipe against the real tree with `emerge --pretend` | ✔ |
| `factory` | build an image's binpkgs in a container | ✔ |
| `assemble` | assemble an image's live ISO from the binhost | ✔ |
| `build` | the factory, then every ISO, in one audited run | ✔ |
| `vm start` / `run` / `test` / `stop` | boot an ISO in a VM and drive it over SSH on vsock | |
| `release` | publish a release (not implemented yet) | |

## Contributing

Issues and pull requests are welcome. Before pushing, run the checks above; the CI job can be
reproduced locally with [`act`](https://github.com/nektos/act) (`act -j quality`).

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).
