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
| Weekly releases (every Sunday, 00:00 UTC) | 🚧 planned: needs a self-hosted runner (Linux with systemd, root) |

Lint, types and tests run on every pull request. The roadmap is in
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

Portage and Gentoo's own tooling are Python, so Shidashi speaks their language: recipes are
validated with pydantic, and the build steps run `emerge` inside the containers and read its
output. Shidashi never imports Portage on the host.

## Requirements

- **To develop and run the tests:** Python ≥ 3.14 and [uv](https://docs.astral.sh/uv/). Any
  Linux works.
- **To build images:** a Linux host with **systemd** and **root**. Everything Gentoo-specific
  runs in a container: `emerge` in the image's stage3, the ISO tools in the **toolbox** (see
  below), the repositories from pins. The host needs no Portage. What it does need:

  | For | Requirement |
  |---|---|
  | building | Python ≥ 3.14, `systemd-nspawn` ≥ 242, GNU `tar` with `--xattrs`/`--acls`, `gpg`, `git`, `openssl`, `objdump` |
  | `shidashi vm` | `qemu-system-x86_64`, `xorriso`, read/write access to `/dev/kvm` and `/dev/vhost-vsock` |
  | optional | `gcc` (names the CPU for the ISA check), `syft` (the SBOM); btrfs under the work directory makes copies instant |

  `shidashi doctor` checks all of it without root, and `pretend`, `factory`, `assemble` and
  `build` refuse to start when a build requirement is missing. A distribution without
  Python 3.14 gets it from `uv python install 3.14`.

So far builds have only run on a Gentoo host: the requirements above come from the code, not
yet from a build on another distribution.

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

On Gentoo, `app-misc/shidashi` from the [Bentoo overlay](https://github.com/obentoo/bentoo)
installs it system-wide, with the recipes and pins under `/usr/share/shidashi`. A checkout
always uses its own `variants/` and `seeds/`, even with the package installed;
`SHIDASHI_VARIANTS_DIR` and `SHIDASHI_SEEDS_DIR` point at any other tree.

### Where the data lives

| Path | What | Override |
|---|---|---|
| `/var/cache/shidashi` | binhost, fork points, pinned trees, distfiles, ccache | `SHIDASHI_CACHE` |
| `/var/log/shidashi/runs` | one audit trail per run | `SHIDASHI_RUNS` |
| `/var/tmp/shidashi` | scratch: build rootfs, logs, VM sessions | `SHIDASHI_SCRATCH` |

`--work-dir DIR` puts all three under one directory instead.

### The builder VM

[`lab/vm/builder.sh`](lab/vm/builder.sh) builds inside a VM booted from a Bentoo `minimal`
ISO, as the guest's root, so the host needs no sudo for `factory` or `assemble`. The host's
cache is shared read-only and an overlay on the VM's own disk takes the writes; logs, audit
trails and ISOs land in `/var/lib/shidashi/vm/builder/out`. It runs from a checkout (the
guest uses the checkout's `.venv`), as a user in the `kvm` group.

```sh
sudo install -d -o "$USER" /var/lib/shidashi/vm   # once: the VM's directory is yours
lab/vm/builder.sh start                     # the newest minimal ISO, 24 vCPUs, 32G, 200G disk
lab/vm/builder.sh setup                     # shares, work disk, overlay
lab/vm/builder.sh job iso-kde build v3 systemd --images kde --output-dir /mnt/out/iso
lab/vm/builder.sh status
shidashi vm test /var/lib/shidashi/vm/builder/out/iso/bentoo-…-kde-systemd-v3.iso   # on the host
```

The factory's privileged tests (`tests/test_factory.py`) skip unless run as root. In the
guest they restore the cached `base` fork point and emerge from the binhost, so point
`SHIDASHI_SEEDS_DIR` at the pins that fork point was built from:

```sh
# as the guest's root, in the checkout
PYTHONPATH="$PWD:$PWD/.venv/lib/python3.14/site-packages" SHIDASHI_CACHE=/work/cache \
  SHIDASHI_SEEDS_DIR=<seeds> python3 -m pytest -p no:cacheprovider \
  --basetemp=/mnt/work/scratch/pytest tests/test_factory.py
```

## Usage

```sh
shidashi doctor                            # can this host build? (no root needed)
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
| `doctor` | check what this host provides for builds and `vm` | |
| `recipe list` / `show` / `validate` | inspect and validate the resolved recipes | |
| `world` | write, check or print each image's package list | |
| `kits check` | check every kit atom against the pinned trees | |
| `pretend` | resolve a recipe against the real tree with `emerge --pretend` | ✔ |
| `factory` | build an image's binpkgs in a container | ✔ |
| `assemble` | assemble an image's live ISO from the binhost | ✔ |
| `build` | the factory, then every ISO, in one audited run | ✔ |
| `vm start` / `run` / `test` / `stop` | boot an ISO in a VM and drive it over SSH on vsock | |
| `kyomei` | pair a worker (a spare machine booted from the `worker` ISO) | |
| `worker disk-init` | turn a disk of a paired worker into its work disk | |
| `release` | publish a release (not implemented yet) | |

### Following a build

`pretend`, `factory`, `assemble` and `build` show their progress on stderr as they run: one
`✓` line per finished step with its time, a line per download and per merged package
(`(47/312) dev-lang/rust-1.91.0  done  41m03s`), and on a terminal a footer with the
running step, a bar over the emerge's packages, the packages in flight and a bar per download.
Piped or redirected, the same lines come out without the footer.

The raw output of every command goes to the run's log, under `/var/tmp/shidashi/logs`
(named when the first command starts). `-v`/`--verbose` prints it on the terminal too. When a
build fails, the terminal shows the last 50 lines of the failing command's output; the log has
all of it.

### Pairing a worker

A worker is a spare machine booted from the `worker` ISO; the host reaches it as root over
SSH. At boot an unpaired worker announces itself on the LAN (mDNS) and shows a one-time code
on its screen. On the host:

```sh
shidashi kyomei --name bentoo-lab          # lists the workers it hears, then asks for the code
shidashi kyomei --address 10.8.0.2         # across networks (a VPN, another subnet)
```

Pick the worker (or confirm the only one), then type the code shown on its screen —
`K7M4-Q2XP`, with or without the dash, in either case. Nothing is typed at the worker. The
host pins the worker's SSH host key under its name, records it in
`~/.local/share/shidashi/worker/`, and proves the pin with one SSH connection. A code lives
10 minutes and is replaced after three wrong tries; `shidashi kyomei` on the worker's console
opens a new pairing window.

The pairing survives a reboot only on a work disk. Prepare one once, naming the disk and
confirming its serial (`lsblk -o NAME,SERIAL,SIZE,MODEL` at the worker's console):

```sh
shidashi worker disk-init bentoo-lab /dev/sda --confirm <serial>
```

It refuses a mounted disk, a serial that is not exactly that disk's, and a second
`SHIDASHI-WORK` disk.

A worker can also pair with nobody at its screen: boot it with this host's kernel parameter
(added in GRUB, or served by network boot), then pair without a code:

```sh
shidashi kyomei --trust-param                      # prints shidashi.trust=<ip>,SHA256:<fp>
shidashi kyomei --trust-param --address 10.8.0.2   # the address the worker will see, via a VPN
shidashi kyomei --trusted                          # pairs only with this host's key and address
```

In trusted mode the worker authenticates the host, but the host cannot authenticate the worker:
compare the fingerprint it prints with the one on the worker's screen.

### Using a worker

A paired worker runs Shidashi commands for the host. Its state first:

```sh
shidashi worker status bentoo-lab     # CPU, targets it can run, load, memory, work disk, SMART, jobs
shidashi worker status                # one line per paired worker
```

A job runs this checkout's committed `HEAD` on the worker, detached from the SSH session:

```sh
shidashi worker job bentoo-lab fac-v3 -- factory v3 minimal systemd
shidashi worker job bentoo-lab minimal-v3 -- assemble v3 minimal systemd --jobs 16
```

Before anything is copied, a job is refused when the checkout has uncommitted changes
(`--allow-dirty` runs `HEAD` anyway), when the worker's CPU cannot run the target arch, when
the worker has no work disk or already runs a job, or when a host directory the results go to
is not writable. Then it pushes the arch's cache (`--bwlimit K` caps the rate), ships the
commit, starts the job, streams its log, pulls the results into
`./worker-results/<worker>/<job>/` (`--results DIR`) and exits with the job's exit code.

A `factory` job, or a `build` that runs the factory, owns the arch's binhost while it runs:
the host's own `shidashi factory` for that arch is refused, and a host `build` skips its
factory. The new binpkgs, fork points and index come home only with that ownership, and it
is released after they do. An `assemble` job only reads the binhost and takes no ownership.

Ctrl+C, or a lost connection, leaves the job running on the worker and prints how to resume:

```sh
shidashi worker logs bentoo-lab fac-v3 -f                 # follow it until it ends
shidashi worker sync pull bentoo-lab --arch v3 --job fac-v3   # bring its results back
```

The cache can also be moved by hand: `shidashi worker sync push bentoo-lab --arch v3` fills
the worker; `sync pull` without `--job` brings back caches only, never the binhost. When the
owner of an arch is gone for good (the worker died mid-job), release it:

```sh
shidashi worker unlock v3             # refused while its job may still run
shidashi worker unlock v3 --force
```

`shidashi worker run bentoo-lab -- CMD…` runs any command there, and
`shidashi worker poweroff bentoo-lab` turns it off (refused while a job runs).

## Contributing

Issues and pull requests are welcome. Before pushing, run the checks above; the CI job can be
reproduced locally with [`act`](https://github.com/nektos/act) (`act pull_request -j quality`).

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).
