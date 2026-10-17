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
  | optional | `gcc` able to name this CPU with `-march=native` (the ISA check of built images; without it the check is skipped and the run's `isa-check` step says so), `syft` (the SBOM); btrfs under the work directory makes copies instant |

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
| `/tmp/shidashi-vm-cids` | vsock CID locks shared by every VM boot on the host | `SHIDASHI_CID_LOCKS` |

`--work-dir DIR` puts all three under one directory instead. `shidashi vm test` keeps its
session (SSH key, QMP socket) in the scratch too: when that directory cannot be written — a
`/var/tmp/shidashi` left by a root build, say — it stops before booting and names
`--work-dir` and `SHIDASHI_SCRATCH` as the way out.

On a node without QEMU — the slim worker image ships none — `vm test`, `assemble --boot-test`
and `build --boot-test` run the whole boot test in a podman container made from the
v3-systemd toolbox, imported as `localhost/shidashi-vm-runner` on first use and reused until
the toolbox changes. It needs root, podman, `/dev/kvm`, `/dev/vhost-vsock` and the toolbox
tarball (`shidashi factory v3 toolbox systemd`, or from the host
`shidashi worker sync push <worker> --arch v3`); `--boot-test` checks all of them before any
build work and names each missing piece with the command that provides it.

The CID lock directory is the same for root and every user, whatever `--work-dir` says;
install [`contrib/tmpfiles.d/shidashi.conf`](contrib/tmpfiles.d/shidashi.conf) in
`/etc/tmpfiles.d/` so root creates it at boot — a copy a user created first (by running
`vm test`) is refused by root's boot tests, and `rm -r /tmp/shidashi-vm-cids` clears it.
`SHIDASHI_CID_LOCKS` must point under a trusted parent: only its last component is checked.

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
| `worker provision` | write a worker ISO that boots already paired | |
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

### Provisioning a worker

A worker is a spare machine booted from the `worker` ISO; the host reaches it as root over
SSH. The simplest way to get one is to give it its identity before it ever boots: the host
writes a personalized copy of the worker ISO that boots already paired, with nobody at the
worker's screen.

```sh
shidashi worker provision bentoo-lab --iso bentoo-…-worker-systemd-v3.iso
sudo dd if=~/.local/share/shidashi/worker/bentoo-lab/bentoo-lab.iso of=/dev/sdX bs=4M conv=fsync status=progress
```

`provision` generates the worker's SSH host key, pins it under the name, records the worker,
and writes the copy to `~/.local/share/shidashi/worker/<name>/<name>.iso` with the identity
in `/shidashi/identity/`, outside the squashfs — the generic ISO stays generic. It prints the
fingerprint and the `dd` line to write the stick. It refuses an ISO that is not a worker ISO
(its volume must be `BENTOO_WORKER`), a name already paired with another key, and a data
directory inside a git work tree; a failure leaves nothing changed.

**The written medium is a credential:** it holds the worker's private host key. Keep it like
one. If it is lost or stolen, provision the name again with `--replace`: the worker gets a
new key and the old one is no longer accepted.

```sh
shidashi worker provision bentoo-lab --iso <worker iso> --replace
```

A worker on Wi-Fi joins its network at boot when its medium carries the connection:

```sh
shidashi worker provision bentoo-lab --iso <worker iso> --wifi "Home Network"
printf '%s\n' "$PSK" | shidashi worker provision bentoo-lab --iso <worker iso> --wifi "Home Network"
```

On a terminal the passphrase is asked twice, without echo; otherwise one line is read from
stdin. The SSID is 1–32 bytes; the passphrase is 8–63 printable ASCII characters or 64 hex
digits, and anything else is refused before anything is written. The passphrase is stored
in the medium's identity (a NetworkManager connection, root-only) and in the host's copy of
that identity, `~/.local/share/shidashi/worker/<name>/identity/wifi/` (0600 in a 0700
directory, beside the worker's private host key) — never in the registry, the output or the
audit trail. Keep both like a key; `--replace` rewrites them. At
boot the worker installs it as `/etc/NetworkManager/system-connections/shidashi-wifi.nmconnection`
(root, 0600); a malformed one is skipped with a message and the wired identity still restores.

To try a provisioned worker before writing a stick, boot its copy in a VM and forward the
guest's SSH port to the host's loopback:

```sh
shidashi vm start ~/.local/share/shidashi/worker/vmworker/vmworker.iso --forward 2222:22
shidashi worker status vmworker --address 127.0.0.1:2222
```

`--forward HOST:GUEST` makes the guest's TCP port `GUEST` reachable on `127.0.0.1:HOST` (never
on another interface), and repeats for more ports. A host port already in use is refused
before QEMU starts.

At boot the worker installs the identity, starts SSH and announces its name on the LAN over
mDNS. The host has no address for it yet: the `worker` commands look the name up over mDNS
(5 s), verify the pinned key, and record the address after the first command that succeeds.
When the recorded address stops answering (a new DHCP lease), `status`, `sync`, `poweroff`
and `job` look the name up again and retry once; `run` and `logs` do not (a command may
already have run), so run `worker status` first. Across networks, where mDNS does not reach, pass the address yourself:

```sh
shidashi worker status bentoo-lab                       # found by name on the LAN
shidashi worker status bentoo-lab --address 10.8.0.2    # HOST[:PORT], recorded once it answers
```

A worker booted from the plain `worker` ISO has no identity: pair it with a code, below.

### The worker image

The `worker` ISO is a build oven, not a desktop: its `system.yaml` declares a `slim:` section,
and its assemble reduces the live system to what a worker runs after the packages are
installed (the other images are assembled as before):

- the world becomes the keep list — systemd, OpenSSH, Python 3.14, podman, git, GnuPG, tar,
  binutils, OpenSSL, btrfs-progs, rsync, gptfdisk, smartmontools, NetworkManager and
  wpa_supplicant, sudo-rs, the kernel, its firmware and microcode, dracut and lvm2 — plus a
  rescue kit (nano, less, iproute2, pciutils, usbutils, nvme-cli, lm-sensors, htop), and
  `emerge --depclean` removes the rest;
- the kernel keeps its network, Wi-Fi, storage, USB and input drivers and drops graphics,
  media, sound, InfiniBand, Bluetooth and a few others; the firmware the remaining drivers
  ask for stays, with CPU microcode and the regulatory database;
- documentation, headers, sources, the compiler's internals, translations and static
  libraries are pruned;
- every command the worker runs (its SSH server, the work disk, the native job runner, the
  rescue kit) is checked present, or the assemble fails naming the missing ones.

Its squashfs has a budget of **600 MiB**: a larger one fails the assemble before that
compression's ISO is written. The slim report (packages removed, driver groups dropped,
firmware deleted, files pruned, sizes before and after) is in the run's audit trail.

### Pairing a worker

At boot an unpaired worker announces itself on the LAN (mDNS) and shows a one-time code on its
screen. On the host:

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

Every worker image is named `shidashi-worker` until it is paired, so give each worker its own
`--name`. A name already paired with another host key is refused, and nothing is pinned: pair
the new worker under another name, or pass `--replace` to replace the old one on purpose
(with `--trusted`, the host still asks you to compare the key with the worker's screen).

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

Once per host, before the first job: host builds run as root and `shidashi worker` as you,
and both write the cache and the audit trails. Give them one group — `portage` here, which
already owns the compiler cache — with setgid directories and a default ACL, so what root's
builds create later stays writable by the group:

```sh
sudo chgrp -R portage /var/cache/shidashi /var/log/shidashi
sudo chmod -R g+rwX /var/cache/shidashi /var/log/shidashi
sudo find /var/cache/shidashi /var/log/shidashi -type d -exec chmod g+s {} +
sudo setfacl -R -m d:g:portage:rwX /var/cache/shidashi /var/log/shidashi
```

Without it a job is refused before anything is copied, naming the first directory you cannot
write and its owner.

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
shidashi worker sync pull bentoo-lab --arch v3 --job fac-v3 --results ~/isos/fac-v3   # elsewhere
```

The cache can also be moved by hand: `shidashi worker sync push bentoo-lab --arch v3` fills
the worker; `sync pull` without `--job` brings back caches only, never the binhost.

Both directions carry only the fork points the checkout's commit can restore: a fork point of
other pins or another build key is skipped and named in the report. Before copying, a sync
measures what it would add on the destination and refuses, copying nothing, when that would
leave less than 10 GiB free there; the job's own record (log, rc, audit trail) is exempt and
always comes back.

When the owner of an arch is gone for good (the worker died mid-job), release it:

```sh
shidashi worker unlock v3             # refused while its job may still run
shidashi worker unlock v3 --force
```

`shidashi worker run bentoo-lab -- CMD…` runs any command there, and
`shidashi worker poweroff bentoo-lab` turns it off (refused while a job runs).

#### An arch whose cache stays on the worker

By default every job brings an arch's binpkgs and fork points home, so the host keeps a full
copy (about 90 GB for v3). To keep that cache on the worker instead:

```sh
shidashi worker cache-owner bentoo-lab --arch v3        # v3's cache now stays on bentoo-lab
shidashi worker cache-owner bentoo-lab                  # which archs bentoo-lab owns
shidashi worker cache-owner bentoo-lab --arch v3 --off  # back under the host
```

For an owned arch, a push sends only the pinned inputs (trees, repositories, stage3), and a
job's pull brings back only its log, exit code, audit runs and ISOs. Binpkgs, fork points,
distfiles and ccache stay on the worker. On the host, `factory`, `assemble` and
`build --local` of that arch are refused, naming the worker and the `shidashi worker job` to
run instead. `build` sends the factory and every image to the owner, and when the owner is
offline nothing is built. Ownership cannot change while the arch's owner lock is held.

Nothing is deleted on either side. The host's earlier copy stays until you remove it:
`/var/cache/shidashi/binpkgs/v3` and `/var/cache/shidashi/fork-points/v3-*`.

### Distributing a build

`sudo shidashi build` splits its images between the host and every paired worker that
answers. It reads the workers paired by the user who ran `sudo` (`SUDO_USER`), not root's,
and each worker's share runs as that user — one `shidashi worker job` per worker — beside the
host's own assembles:

```sh
sudo shidashi build v3 systemd --plan     # print the split and the estimates, build nothing
sudo shidashi build v3 systemd            # build it: host and workers at once
sudo shidashi build v3 systemd --local    # the host alone, whatever answers
```

The split minimizes the time the slowest node takes, estimated from each node's own audit
runs (its last successful assemble of the image, else its seconds per package, else 1.7 s),
scaled by its free threads; a trunk a node already holds costs nothing there, and the desktop
flavors stay together unless splitting them wins. A worker is left out — and named, with why —
when it does not answer within 10 s, has no work disk, already runs a job, or its CPU cannot
run the arch. Every build records its plan in the audit trail before it starts.

A worker builds the committed `HEAD`, the host its working tree, so a build that gives a worker
any image is refused while the checkout has changes, untracked files included;
`--allow-dirty` builds anyway. The workers' ISOs, their companion files and build bundle land
in the output directory beside the host's, owned like it, with their `SHA256SUMS` lines
computed on the host. Ctrl+C prints, per worker, the `worker logs … -f` and
`worker sync pull … --job …` commands that follow and bring back a job left running.

A distributed build takes hours, so run it detached from the terminal — closing the terminal
stops the build (exit 129). Refresh sudo first, so the detached command does not wait for a
password:

```sh
sudo -v && sudo nohup shidashi build v3 systemd > /tmp/build.log 2>&1 &
# or inside tmux: tmux new -s build 'sudo shidashi build v3 systemd'
```

If the terminal hangs up anyway, the workers keep building: the build's audit run records an
`interrupted` step with each worker job's commands, and its results come back with
`shidashi worker sync pull <worker> --arch <arch> --job <job>`.

## Contributing

Issues and pull requests are welcome. Before pushing, run the checks above; the CI job can be
reproduced locally with [`act`](https://github.com/nektos/act) (`act pull_request -j quality`).

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).
