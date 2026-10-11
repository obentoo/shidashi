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
- **An assemble resumes after a failure instead of installing again.** The install
  is 80% of an assemble (11 minutes for minimal, 44 for kde), and every failure came
  after it. On a btrfs scratch the image's rootfs is frozen into read-only snapshots
  after the install and after the final package set; a failed install keeps
  Portage's own resume list, so the next run merges only what was left. The
  checkpoints (`install`, `packages`, `install-partial`) are content-addressed and
  shared by every image of an arch and init: their fingerprint is the rendered
  configuration, the stage3, the profile, the pins and the install's command line,
  so an image whose layers configure nothing new resumes from another's (the worker
  from minimal's, in 96 s). The binhost is checked apart, by what the image uses: an
  `install` or `packages` checkpoint is reused while the index entries of its own
  packages are unchanged and `emerge --pretend` still chooses the same binpkgs, so a
  binpkg rebuilt for kde leaves minimal's alone (minimal resumed in 103 s after such
  a change, against ~13 minutes whole); `install-partial` skips that check, since
  fixing the binhost is how a failed install is repaired. A resumed image is the
  image a clean run makes: the same 475 packages, checked on a real worker assemble
  killed after its install. `--fresh` restores no checkpoint, not one another image
  still holds nor the trunk, drops the image's own and installs from the stage3. A
  binpkg that fails its checksum is named instead of ending in Portage's traceback.
  Off btrfs nothing changes.
- **The worker image: a spare machine as a build oven.** A console image on
  minimal (`base → minimal → worker`), booted like any Bentoo medium (its menu's
  copy-to-RAM entry frees the stick), with key-only sshd that starts only once a
  key is in place. Pairing takes two commands: `lab/worker/kyomei.sh [-n NAME]` on
  the host serves the host's worker key (and, with `-n`, the worker's hostname) for
  30 minutes, and `shidashi kyomei <host IP>` on the worker's console fetches it,
  allows it for root and starts sshd. **The pairing is plain HTTP for now:** it
  authenticates neither side, so both ends print the key's fingerprint for a person
  to compare; story 009 replaces it with an authenticated protocol.
- **A worker that boots already paired (story 020).** `shidashi worker provision N
  --iso <worker iso>` gives worker N its SSH host key before it ever boots: it pins
  the key, records N, and writes a personalized copy of the generic worker ISO with
  the identity in `/shidashi/identity/`, outside the squashfs, root-only on the
  medium — the generic ISO stays generic. Write the copy to a stick (`dd`, printed
  by the command) and the worker installs the identity at boot, starts sshd and
  announces its name over mDNS, with nobody at its console; the work disk's pairing
  and the pairing window are skipped. The `worker` commands then find N by name
  (5 s), verify the pinned key and record the address after the first command that
  succeeds; `status`, `sync`, `job` and `poweroff` look N up again and retry once
  when its recorded address stops answering (`run` and `logs` do not: a retry could
  run the command twice). `--address HOST[:PORT]` skips the lookup. The first
  `worker status` or `worker job` fills a provisioned worker's CPU flags and image,
  the job before its CPU check. A refused or failed provision changes nothing; a
  lost medium is revoked with `--replace`, which re-keys N. **The written medium is
  a credential**: it holds the worker's private host key. Checked on a VM (default,
  reboot and copy-to-RAM boots) and on a real worker reached by name, twice.

### Changed

- **kde, gnome and wm grow from a shared trunk.** Their `desktop` stage is
  identical, so the assemble installs it once, straight from the stage3, keeps it as
  a checkpoint and branches each flavor from it with `--update --deep --newuse`; kde
  took 630 s on the trunk against 2179 s whole (the trunk itself, 1163 s, is paid
  once). The result was checked against a whole install: the same 1061 packages
  (version, build id, USE, files) and boot test 50/50, the differing files being
  only what each build generates. For the checkpoints to be shared, only the base's
  `rootfs/` lands before the install; every other layer's files land after it, in
  the new `rootfs` step. `--no-trunk` installs a flavor whole from the stage3.
- **Package selection.** vim, bash and their completions leave the `shell` kit for
  the binhost catalog (bash stays in every image through `@system`; nano is the
  installed editor); GNOME's USE gains `gtk4`; KDE's drops `qt5`.
- **`::gentoo` re-pinned to 20260928, `::bentoo` to the same day.** The 20260919
  snapshot rotated off the mirror, and 20260926 a day after it was pinned.

### Fixed

- **The generation guard refused a gcc patch release, checked only once and restored
  trees of an older pin (story 016).** The pilot's `base` phase restored a bootstrap
  checkpoint built under an older `::gentoo` pin, upgraded gcc `16.2.0` →
  `16.2.1_p20260926` and wrote its binpkgs into the PKGDIR it had just verified
  (audit run `20261005T205604Z-f0f216`); the resume was then refused on that
  ABI-neutral change (`20261005T215023Z-e30ebd`). The fingerprint is now compared by
  ABI (a new gcc major or a step down within one ends the generation, glibc may only go
  up, binutils is not compared; an accepted gcc or glibc upgrade raises the recorded
  floor, so a later step back is refused), re-checked
  after every phase's emerge and after the update's, and a refusal names the successor
  `--pkgdir`. `factory --update` refuses a plan by the same rules, binpkg lines
  included. The bootstrap checkpoint, the stage fork points, the toolbox and the
  per-phase snapshots are keyed by the repository pin id and a build key (a hash of the
  profile, CFLAGS, RUSTFLAGS, CPU_FLAGS_X86 and GOAMD64) too, so a flags change never
  restores a tree built with the old flags. **After upgrading, the
  first factory build rebuilds the bootstrap and the stages once, even under unchanged
  pins** (the old restore points carry no pin id and are left on disk), a stepwise
  state saved before the upgrade is stale (`--reset`), and the assembler needs the
  toolbox rebuilt under the current pin.
- **`sudo shidashi pretend` refused the stage3 with "No public key".** The
  `.DIGESTS` signature was checked against the caller's own keyring (root's, empty).
  It is now checked like the `::gentoo` snapshot: against Gentoo's release keys in
  a throwaway keyring, accepted only on GOODSIG and VALIDSIG. A keyring that imports
  nothing is named instead of surfacing as a missing key.
- **An image could hold packages its pins do not have.** `--usepkgonly` makes emerge
  ignore the ebuild repositories, so the assemble took the newest binpkg of the
  binhost, even a version the pinned tree lacks, and ignored the tree's masks; the
  binhost gathers every pin built on the same stage3. A minimal ISO pinned to
  gentoo-20260919 shipped 4 such packages, and 28 in the builder VM (systemd-262,
  gentoo-kernel-bin-7.2.8). The assemble now resolves with
  `--use-ebuild-visibility=y`, and stops, naming them, if a planned binpkg still has
  no ebuild in the pinned trees. Images published before this may hold such
  packages.
- **`factory --until` with an invalid name did work before refusing it.** It seeded
  the rootfs, ran the generation check and wrote the build state first; it is now
  refused right after the root guard.
- **A worker job's binpkgs could be left on the worker.** The push and the pull worked
  the binhost generation out of the host checkout's pins, not the shipped commit's: a
  pin bump while a job ran made the pull report "built no binpkg" and release the lock.
  The job now records its commit's generation in the owner lock and both use it.
- **Two writers of one binhost during a pull.** `worker unlock` without `--force` let a
  host build start while the host was still pulling a finished job's results, and the
  pull's index replaced the build's. A pull now marks itself; `unlock` refuses while it
  runs and a second pull of the arch is refused.
- **Pairing a second worker could silently replace the first.** Every worker image is
  named `shidashi-worker`; a name already paired with another host key is now refused,
  with nothing pinned, unless `--replace` (a `--trusted` replace always asks to compare
  the key). A paired worker whose disk appeared late at boot opened a new pairing window
  instead of restoring its pairing: the restore now waits for the work disk's mount.
- **A build-time secret pattern could match nothing.** A pattern with a trailing `/`,
  `./`, `//` or an empty segment loaded and silently matched no file; it is now refused
  when `livecd.yaml` loads.
- **A toolbox, checkpoint or OS error ended in a traceback** in `assemble`, `build`,
  `factory` and `pretend`. It is now an `error:` line and exit 1, naming the path.
- **The factory merged binpkgs linked against a library the pinned tree no longer
  ships (story 019).** A binhost kept `x11-libs/vte` and `net-libs/nodejs` built
  against `libsimdutf.so.34` while the pinned simdutf installs `.so.36` under an
  unchanged subslot, so `gnome-terminal` failed to link. Before each stage's emerge,
  the factory now judges the plan against the binhost index: a binpkg whose built
  slot-operator subslot, or whose required soname, no longer matches what the plan's
  providers offer is compiled again from its ebuild (providers first when needed),
  the stale instance moves to the cache's quarantine and the index is regenerated;
  the stage's report names each one with the two subslots or sonames. A binhost with
  nothing stale runs the emerge exactly as before. `assemble` and the factory's
  `check-binpkgs` refuse a stale plan instead of installing it, and a flavor growing
  from a trunk with no checkpoint has both plans judged before the trunk installs.
- **`worker sync pull` filled the host's disk.** It copied every fork point of the
  worker, orphans of other pins included. Both directions now carry only the fork
  points the checkout's commit can restore (the others are named in the report), and
  a sync that would leave less than 10 GiB free on its destination copies nothing and
  says so; a job's own record (log, rc, audit trail) always comes back.
- **`shidashi vm test` and `vm start` crashed when their scratch was root-owned** (a
  `/var/tmp/shidashi` left by a root build). They now stop before booting and name
  `--work-dir` and `SHIDASHI_SCRATCH`.

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
