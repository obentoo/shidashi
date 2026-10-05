# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The gnome image ships GNOME: `gnome-light` and GDM.** Its kit was a curation
  placeholder with no atoms, so the gnome ISO booted to a tty with the desktop
  trunk but no shell and no display manager. `kits/desktops/gnome` now installs
  `gnome-base/gnome-light` (the shell, the session, Settings, Files and a terminal)
  and `gnome-base/gdm`, without `gnome`'s full application set; the image picks its
  own applications. `flavor/gnome/system.yaml` enables `gdm.service` under systemd
  and the `display-manager` service under OpenRC, so the boot test also checks the
  display manager and the graphical autologin. On the Wayland-only base, the
  gnome flavor turns X on for GTK 4, vulkan-loader and mesa (IBus's input methods
  need `gtk:4[X]`; still no X session), builds SpiderMonkey with GCC instead of a
  second LLVM, and leaves the Wacom panel out of Settings, which would have pulled
  an X server into the image.
- **A build shows its progress, pacman-style.** `pretend`, `factory`, `assemble`
  and `build` were silent for hours. They now print on stderr a `✓` line per
  finished step with its time, a line per download and per merged package and, on
  a terminal, a footer with the running step, a bar over the emerge's packages,
  the packages in flight and a bar per download. `-v`/`--verbose` echoes the raw
  output too; it always goes to the run's log, whose path is printed. A failure
  shows the last 50 lines of the failing command instead of all of it. `pretend`
  gains a log of its own.

### Changed

- **Package selection.** vim, bash and their completions leave the `shell` kit for
  the binhost catalog (bash stays in every image through `@system`; nano is the
  installed editor); GNOME's USE gains `gtk4`; KDE's drops `qt5`.
- **`::gentoo` re-pinned to 20260926, `::bentoo` to the same day.** The 20260919
  snapshot rotated off the mirror.

### Fixed

- **`sudo shidashi pretend` refused the stage3 with "No public key".** The
  `.DIGESTS` signature was checked against the caller's own keyring (root's, empty).
  It is now checked like the `::gentoo` snapshot: against Gentoo's release keys in
  a throwaway keyring, accepted only on GOODSIG and VALIDSIG. A keyring that imports
  nothing is named instead of surfacing as a missing key.

## [0.1.1] - 2026-10-03

Builds no longer depend on the host being Gentoo.

### Added

- **The ISO is made in a Bentoo toolbox, not with the host's tools.** `mksquashfs`,
  `grub-mkrescue` and `unsquashfs` ran on the build host, so the ISO carried the
  host's GRUB — a version no pin recorded — and the host had to provide them under
  its own distribution's names. A `toolbox` stage (`base → toolbox`) installs GRUB
  for BIOS and UEFI, libisoburn and mtools; `assemble` runs the tools in its fork
  point under `systemd-nspawn`, with the image read-only, and records their versions
  in the medium's `bentoo/build.json`. `shidashi build` builds the toolbox first.
- **`shidashi doctor`: what the build host must provide.** Python ≥ 3.14,
  `systemd-nspawn` ≥ 242, GNU tar with `--xattrs`/`--acls`, gpg, git, openssl and
  objdump for a build; QEMU, xorriso, `/dev/kvm` and `/dev/vhost-vsock` for
  `shidashi vm`. `pretend`, `factory`, `assemble` and `build` run the same check
  first and refuse to start, naming what is missing.
- **`shidashi vm` attaches disks and shares directories.** `--disk` (a sparse
  qcow2, created with `--disk-size`) and `--share TAG=DIR[:rw]` (virtiofs, one
  `virtiofsd` per share). A read-only share shows the host's real owners; what the
  guest's root writes to a writable share belongs to the invoking user, never to
  the host's root. `vm run` gains `--timeout`.
- **A builder VM: builds as the guest's root, no sudo on the host.**
  [`lab/vm/builder.sh`](lab/vm/builder.sh) boots a Bentoo `minimal` ISO, shares the
  host's cache read-only under an overlay on the VM's own disk, and runs `shidashi`
  jobs detached, with their logs, audit trails and ISOs written back to the host.
- **A `perl-rebuild` step before each stage's emerge.** The base upgrades Perl and
  rebuilds the world, but the stage3's build-only modules stayed in the old Perl's
  directory, where the new one never looks — the toolbox's GRUB failed when
  `help2man` could not load `Locale::gettext`. Packages owning modules of a Perl
  that is not installed are rebuilt from source, and the stage fails if any remain.
- **An `exclude:` cannot reach into an earlier stage's kits.** Fork points are
  shared: a flavor excluding an atom that `minimal` installs would change
  `minimal`'s fork point only when that flavor built it. Such an exclude is refused,
  naming the stage that installs the atom and where the exclude belongs.
- **An installed Shidashi finds `/usr/share/shidashi` by itself.** `variants/` and
  `seeds/` resolve to the environment, then a checkout's own tree, then the system
  install, so the ebuild needs no `env.d` — which used to point every shell, a
  checkout included, at the installed recipes.

### Changed

- **The host needs Linux with systemd, not Gentoo.** The README and OVERVIEW list
  the host's requirements as `shidashi doctor` checks them; Portage runs only in the
  containers.
- **Repositories come only from pins.** A repository declared in `repos.conf`
  without a pin in `seeds/` is an error; the host's `/var/db/repos` is never read.
- **`factory --jobs N` also sets emerge's jobs.** It wrote only `MAKEOPTS`, so emerge
  built one package at a time; it now also sets
  `EMERGE_DEFAULT_OPTS="--jobs=N --load-average=N"`. In `assemble`, `--jobs` also
  caps `unsquashfs` and the stage4's `xz`.
- **Out-of-tree kernel modules are built unsigned.** `modules-sign` left the global
  USE: the images run `gentoo-kernel-bin`, whose signing key is never shipped, so
  every out-of-tree module (`nvidia-drivers`, `r8168`, `virtualbox-modules`…) died in
  `pkg_setup`. The kernel loads them unsigned; signing them for Secure Boot needs the
  user's own key.
- **The scratch directory defaults to `/var/tmp/shidashi`.**

### Fixed

- **The build host's `--jobs` reached the ISO's `make.conf`.** `build --jobs` set
  `MAKEOPTS` for the whole run, and the image installed the build host's job counts;
  the assembler leaves them out.
- **The toolbox could not be built under systemd.** `init/systemd`'s excludes
  target kits that only images install; a chain that ends in no image may now miss
  them.

### Removed

- **The Catalyst seed mode.** The generic stage3 is only a seed, and the toolchain
  bootstrap and every stage already rebuild it with the arch's `-march`; no arch
  enabled it.
- **`shidashi.portage_api`.** It guarded an `import portage` on the host that
  nothing called.

## [0.1.0] - 2026-10-01

First release: Bentoo images and live ISOs built from an official Gentoo stage3,
stage by stage.

### Added

- **Package Factory (`shidashi factory`).** A verified stage3 bootstraps its
  toolchain and grows through `base → minimal → desktop → <flavor>` in
  `systemd-nspawn` containers. Every package lands in a binhost as a binpkg, and
  each stage leaves a fork point the next builds resume from; `--step`, `--until`,
  `--stop-after` and `--update` drive a build stage by stage or update it within
  its generation.
- **ISO Assembler (`shidashi assemble`).** The live ISO, hybrid BIOS and UEFI, is
  installed from the binhost only, configured (hostname, locale, services, the live
  user), depcleaned and published with its digests, package list, contents and SBOM.
- **Boot test (`shidashi vm test`).** Boots an ISO on BIOS and UEFI, reaches it over
  SSH on vsock without root, and checks what its recipe declares.
- **Recipes as data.** Images combine arch × flavor × init under `variants/`, built
  from a library of kits (`#atom` lines go to the binhost only), with `exclude:` and
  `include:` per stage; the process itself is data in `flow.yaml`, and per-package
  exceptions in `quirks.yaml`.
- **Reproducible inputs.** The stage3, a week-old `::gentoo` snapshot and the
  overlays are pinned; a generation fingerprint keeps binpkgs of two toolchains apart;
  every run leaves an audit trail.

[Unreleased]: https://github.com/obentoo/shidashi/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/obentoo/shidashi/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/obentoo/shidashi/releases/tag/v0.1.0
