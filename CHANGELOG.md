# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`shidashi vm ssh-config`: open a dev VM in an editor.** It writes a `Host`
  (`shidashi-NAME`) beside the session that lands as root in the guest, so an editor's
  remote project (Zeo, Zed, VS Code) or a plain `ssh` reaches it; one `Include` of
  `~/.local/state/shidashi/vm/*/ssh_config` covers every session. A VM on this host is
  reached through `systemd-ssh-proxy`; a VM on a build worker through the pinned worker,
  whose hidden `vm ssh-relay` joins the channel to the guest's sshd on vsock (the file
  descriptor `systemd-ssh-proxy` passes cannot cross that hop). A client key made on the
  host is authorized in the guest; it and the `Host` go when the session is stopped.
  Proven on bentoo-lab with Zeo's agent running in the guest (2026-11-16).
- **`shidashi vm env NAME`: a variable for the guest's login shells, from stdin.** The
  value is never a command-line word: it is kept in the guest's RAM
  (`/run/shidashi-env/NAME`, 0600) and exported by `/etc/profile.d/shidashi-env.sh` --
  the environment an editor's remote server and its agents start from. A value typed at a
  terminal is refused, since it would be echoed.
- **One CPU and memory budget shared by every consumer of a worker.** Worker jobs, CI
  VMs and dev VMs now reserve their share through `contrib/worker/slot` before they
  start, beside the `lab` tool and Zeo's `lab.sh`, which call the same script at
  `/mnt/work/slots/slot` (published there by every Shidashi run). Jobs and CI VMs wait
  their turn; a dev VM that does not fit is refused at once, the holders named. The
  reservation lasts as long as its command — a dev VM's QEMU inherits it — and a dead
  one is swept by the next run. Before, each consumer had a lock of its own and a Zeo
  build could run beside a CI VM. Proven on bentoo-lab (2026-11-17): a dev VM's share
  held by QEMU, a second VM refused, the share freed by `vm stop`; a `lab` run queued
  behind a full budget; a Zeo build reserving 16 CPUs / 30 GiB. See the README's
  "Sharing a worker".
- **A floor and a report for the worker's disk.** `slot run` refuses to start while the
  work disk has less than 30 GiB free, for every consumer at once; `slot du` reports what
  each consumer holds (Shidashi's trees, Zeo's `zeo-lab`, `lab`, podman's images, swap).
- **Superseded runner images are pruned.** Importing a new
  `localhost/shidashi-vm-runner` image keeps it and the one before, and removes the
  older ones (not one a container uses); nothing removed them before — eight, ~50 GB, on
  bentoo-lab on 2026-11-16.

### Fixed

- **The session key reaches the guest's `authorized_keys` ended by a newline.** systemd
  writes the `ssh.authorized_keys.root` credential byte for byte, and it was sent without
  one: a key added after it joined its line, and neither key worked.

## [0.4.0] - 2026-11-16

The worker becomes a shared build machine: bentoolkit proves ebuild bumps in disposable
VMs there, and private repositories' GitHub Actions jobs run on it, one VM per job.

### Added

- **The worker runs private repositories' GitHub Actions jobs, one disposable VM per
  job** (`shidashi ci pool`, story 034). A poller on the host mints a just-in-time runner
  for each queued job carrying the configured label (the token never leaves the host); the
  worker boots a copy-on-write overlay of a pinned Ubuntu image (`seeds/ci-guest.toml`)
  with Docker and, when the worker's KVM allows nesting, `/dev/kvm`; the VM takes that one
  job, powers off and its overlay is deleted. Only private repositories are served. The
  poller ships as a user unit, `contrib/systemd-user/shidashi-ci-pool.service`, not enabled.

- **`shidashi fork-points gc <arch>` deletes the fork points of old pins.** Every re-pin
  writes a new set and nothing deleted the old ones: the v3 worker's disk was at 90% with
  two superseded pins' tarballs on it, and deleting the older pin's freed 33 GiB. The gc
  keeps the current pin's and those of the `--keep` newest other pins (default 1, the pin
  the update and a reverted re-pin restore); `--dry-run` prints the plan and takes no
  lock. The sizes it prints are the tarballs' apparent ones: a compressed filesystem
  frees less (79.8 GiB of tarballs freed those 33 GiB). A worker-owned arch is
  collected on that worker, and the weekly lab cycle runs the gc before each build
  (`WEEKLY_FORK_POINTS_KEEP`; empty turns it off).
- **`shidashi vm start --dev`: a disposable VM to build ebuilds in, for bentoolkit.** It
  boots the newest minimal ISO with the trees that ISO was built from, a fresh work disk,
  an empty `/var/db/repos/staged` repository and the available binhosts, and prints one
  JSON line (exit 3: no ISO to boot).
- **`shidashi vm start --dev` also finds ISOs kept outside the releases.** Besides
  `<cache>/releases/<date>/` it reads a stash, `/var/tmp/shidashi-isos`
  (`SHIDASHI_DEV_ISO_DIR`), flat or one directory down, so releases can be pruned without
  naming `--iso` every time. The newest by the date in the file name is booted; a release
  wins a tie. The stash is searched only when it is the invoking user's alone (a real
  directory, not a symbolic link, not writable by group or others): anyone can create a
  directory under `/var/tmp`. One that is not is skipped, with a warning on stderr and the
  reason in the exit-3 error.
- **`shidashi vm run --stream`** passes the guest's output through as it is written;
  **`--timeout 0`** waits however long the command takes. stdin reaches the guest command
  in both modes.
- **`shidashi vm start --dev --max-life HOURS`: a dev session that ends itself.** A
  session whose caller died kept a VM and a work disk of up to 40 GB alive, and nothing
  stopped it: the sweep of a later start only removes sessions whose QEMU is already
  dead. With `--max-life` (a positive number, fractions allowed; without `--dev` a usage
  error) the session gets a deadline, counted from the moment the VM is ready, and a small
  detached watchdog that then does what `vm stop` does — VM off, directory removed. No
  systemd is involved: it works for a user on the host and for root on a worker alike.
  The JSON line gains `"expires"`, the deadline in ISO 8601 UTC, `null` without the
  option. `vm stop`, the sweep and a start under the same name end the watchdog of the
  session they remove. Each expiry appends one JSON line to `expiry.jsonl` beside the
  session directories (`$XDG_STATE_HOME/shidashi/vm/`, or `$SHIDASHI_SCRATCH/vm/`); a
  session that cannot be ended is logged as `failed` and tried again every minute.
- **`shidashi vm start --dev --pins-json` (hidden) takes the ISO's pins from the caller.**
  An ISO assembled before build.json named its pins has them read with git from the
  Shidashi checkout, and a build worker runs a shipped source tree with no `.git`. The
  host resolves them and hands them over as the same block build.json carries
  (`iso_pins.pins_block` writes it). git is then never asked; an ISO that does name its
  pins must agree with the ones handed in, or the start fails saying how they differ. A
  block that does not parse, has the wrong shape or carries an id that is not its own is
  a usage error (exit 2) before anything boots.
- **`shidashi vm start --dev --worker NAME` runs the build VM on a paired worker.** A
  worker has what a developer's machine lacks -- bentoo-lab: 46 GB, 16 threads, the binpkg
  pool on its own disk -- and bentoolkit must not change to use it. So the host does not
  grow a second way to run a VM: it ships its checkout's `HEAD` and runs the SAME
  `vm start --dev` there over ssh, then prints that command's JSON line with
  `"worker": "NAME"` added. `SHIDASHI_DEV_WORKER` names the worker when `--worker` does
  not; `--local` runs the VM on the host whatever the variable says (both options
  together, or either without `--dev`: exit 2). `vm run` and `vm stop` take no new
  option: the start leaves `worker.json` -- the worker and the commit it shipped -- in the
  session's directory under `$XDG_STATE_HOME/shidashi/vm/<name>/`, and a session recorded
  there is reached on its worker, with that commit's tree, whatever `HEAD` has become.
  - Refused before anything is shipped, exit 1 and one line: a worker that does not
    answer, **a job running there** (it is named), no work disk, a session of that name
    alive there, alive on the host, or recorded on another worker. No ISO to boot is
    still exit 3.
  - The ISO is chosen on the host by the rule of a local start and kept on the worker
    under `/mnt/work/cache/dev-isos/`: rsync sends it once (2.1 GB in 18 s to bentoo-lab)
    and a later start finds it there by size and time. Its pins are resolved on the host
    and handed over (`--pins-json`): the shipped tree has no `.git`.
  - On a worker the VM gets **24G** unless `--memory` says otherwise (the guest's RAM root
    is 4.7 GB then, 1.6 GB at 8G), the worker's own thread count unless `--cpus` does (the
    guest builds with a fixed `-j32`: its processors are the limit), and a **maximum
    life of 12 hours** unless `--max-life` does -- a session nobody stops does not hold a
    worker's memory for ever.
  - `vm run` passes stdin, stdout and stderr straight through (no terminal) and exits
    with the command's code, 124 past its `--timeout` -- enforced on the worker -- and
    ssh's own 255 when the worker cannot be reached. Stopped by Ctrl+C, SIGTERM or
    SIGHUP it ends the worker's `vm run` by its process group, which stops the command
    in the guest, before it exits 128 + the signal (measured: 0.4 s).
  - `vm stop` stops the session there and forgets it; a session the worker no longer has
    (its life ended, the worker restarted) is said on stderr and forgotten, exit 0; a
    worker that does not answer is exit 1 and the record stays.
  - A start that fails, or is stopped, undoes what it began on the worker and leaves no
    record. One killed outright -- bentoolkit cancels with SIGKILL -- leaves the record,
    written before the VM is started, so the `vm stop` that follows ends the start still
    under way.
  - A start and a stop on a worker are audited runs on the host (`vm-dev-worker`,
    `vm-dev-worker-stop`): the probe, the commit shipped, the ISO `sent` or `reused` with
    its bytes, the line sent, the durations.

  Measured on bentoo-lab (2026-11-16): the first start, ISO included, 45 s; the next one
  25 s (23 s of it the boot); a `vm run` round trip 0.9 s; a stop 0.8 s.
- **A `worker job` stops the dev VMs on its worker first.** A job is what the worker is
  for, so it neither waits for a dev session nor runs beside one holding 24G: its opening
  probe lists the dev sessions there and, once the job's tree is shipped, that tree's
  `vm stop --every-dev` (hidden) ends them -- VM off, directory and work disk removed. The
  job's audit trail names them (`worker.job.dev_sessions_stopped`); a session that will
  not stop refuses the job. The host that started such a session learns it at its next
  `vm run` (exit 1, in words) or `vm stop` (exit 0, forgotten).

### Changed

- **`shidashi vm start --cpus` tells "not given" from `8`,** as `--memory` does: the VM
  still gets 8 processors on the host (`vm.DEFAULT_CPUS`), and a worker gives it its own
  thread count.
- **`shidashi vm start --memory` tells "not given" from `8G`.** The option has no default
  at the command line any more; the VM still gets 8G (`vm.DEFAULT_MEMORY`) where it is
  made, and the help still says so. A caller that starts the VM elsewhere -- a build
  worker with more RAM -- can now pick its own default without overriding a choice.
- **The worker image ships QEMU, virtiofsd and xorriso.** The worker is to host
  bentoolkit's build VMs (`vm start --dev`), next to the binpkg pool and with its 16
  threads. They cannot be added to a worker that is running: the pool's binpkgs belong
  to the current pin, and on an image of an older one the install wants a new glibc and
  OpenSSL under a live sshd. xorriso comes with QEMU because a node that has
  `qemu-system-x86_64` runs its boot test natively, and that path reads the ISO with it;
  the toolbox container stays the way on a worker booted from an older image. The slim
  step keeps the three packages and proves their commands; measured on the v3 toolbox,
  they add about 34 MiB to a 533 MiB squashfs, inside the 600 MiB budget.
- **The stale-binpkgs check asks Portage in one interpreter, not one `portageq` per
  question.** Each `portageq` loaded the configuration and the repositories again: the v3
  index's 677 slot-dep queries cost ~4 minutes in every stage of every build. The stage's
  own `portageq` script is now loaded once and its `main()` called per query, so the
  answers are still the installed Portage's -- byte for byte the same over the 677 real
  queries, in 2.2 s instead of 203 s. A stage whose `portageq` cannot be loaded that way
  is asked one query at a time, as before, and the audit trail records
  `binpkgs.portageq_per_query`.

### Fixed

- **A `vm run` ended by SIGTERM or SIGHUP no longer leaves its command running in the
  guest.** The command was stopped in the guest on its timeout and on Ctrl+C only: the
  default action of the two signals ended `vm run` where it stood, and a build went on in
  the VM. That is how a `vm run` ends when it is driven over ssh and the connection goes,
  or when its process group is killed. While a guest command runs, both signals now
  unwind as Ctrl+C does -- the guest's process group is killed, the local ssh is reaped --
  and the process exits with 128 + the signal (143, 129). Once the stop is under way a
  second signal is ignored, the one that kills the guest command included; a SIGHUP
  ignored on purpose (`nohup`) stays ignored.
- **A package with a fresh binpkg is no longer compiled again in every build because an
  old version of it has a stale one.** The `stale-binpkgs` step excluded the whole
  package (`--usepkg-exclude=cat/pkg`) until a fresh binpkg of the stale *version*
  appeared. A version the tree no longer picks is never rebuilt, so it never got one and
  never left the pool: `dev-lang/python-3.14.10` and `-3.15.2`, built against OpenSSL's
  old subslot, kept `dev-lang/python` excluded in every stage of every build, and the
  current `python-3.15.2_p1` was compiled each time (six identical builds in the pool, one
  per run). In one toolbox run 148 packages were excluded and 76 of them had a fresh
  binpkg of some version (qtbase, spidermonkey, gnome-shell, ruby, git…). The rule is now
  per slot: the step asks the stage's tree which version it picks in the slot of each
  version left with no fresh binpkg. If it is that version, only the slot atom
  (`cat/pkg:SLOT`) is excluded and its stale binpkgs move aside after the rebuild, as
  before. If it is another version, or nothing is visible there, nothing will ever rebuild
  it: its stale binpkgs move to the quarantine at once and nothing is excluded. The
  exception is a version the stage's own plan merges while the tree picks another (found
  by the soname rule): its stale binpkgs move at once *and* its slot is compiled that one
  time. In the step's audit data `excluded` now lists slot atoms, and `superseded` lists
  what was moved aside for this reason, with the version the tree picks instead (`null`:
  nothing visible). The resolver's answer lines gained the version as a third field, in
  both the one-interpreter and the per-query form. Proven against this host's Portage
  3.0.83 (a slot atom leaves the other slot's binpkg merged); not yet run against the
  real binhost.
- **A soname no rebuild can clear no longer fails the weekly release.** The release of
  2026.11.13 stopped at `sys-kernel/gentoo-kernel-bin-7.3.3 (build 1): needs
  libcrypto.so.3 — offered libcrypto.so.4`: the package ships `certs/extract-cert`
  prebuilt against OpenSSL 3 and its ebuild does not rebuild it, so the rebuild the
  refusal asked for wrote the same binpkg again. `variants/soname-waivers.yaml` now
  lists such findings -- one exact package and one exact soname per entry, with the
  reason, the evidence and the exit condition, all required. A waived finding is not
  stale in the factory's `stale-binpkgs` step, in `check-binpkgs` or in the assemble;
  the subslot rule, the unresolved sonames and any other soname of the same package
  are judged as before. Each judgement records what it waived and the waivers that
  matched nothing (`soname_waived`, `soname_waivers_unused`: in the `stale-binpkgs`
  step data, and in a `binpkgs.soname_waivers` event for the other two), so a waiver
  whose exit condition is met shows up as unused and can be deleted. Not yet run
  against the real binhost.
- **Every stage built from the base recompiled ~20 Perl modules, on every run.** The
  base upgrades Perl inside its own emerge, and `perl-rebuild` ran only before it: the
  stage3's build-only modules stayed in the old Perl's directory and the base's fork
  point was saved that way, so each stage restored from it rebuilt their owners from
  source (2m34 per toolbox run, 53 identical builds of each module in the binpkg pool).
  The flow now takes `perl-rebuild` twice -- before the stage's emerge, as before, and
  right after it, before the fork point -- and refuses any other placement. A base fork
  point already on disk keeps the stale modules until the base is built again; until
  then the step before the emerge repairs each stage, as it did.
- **`worker provision --replace` keeps the arch caches the worker owns.** The replaced
  entry's `cache_archs` was dropped, so the host went back to building those archs itself
  and to syncing a cache that lives on the worker only. The address, CPU flags and image
  still start over: they are the new medium's.
- **Re-pairing a worker with `shidashi kyomei` keeps the arch caches it owns.** It had
  the same defect: the new entry dropped `cache_archs`. When the worker answers with
  another host key (`--replace`), which may be another machine, the command says how to
  hand those archs back to the host (`worker cache-owner NAME --arch ARCH --off`).
- **A GitHub token file of mode 0400 is accepted.** `shidashi ci` required exactly 0600
  and refused a file stricter than that. It now takes any file of yours that group and
  others cannot read or write; one with a setuid, setgid or sticky bit is still refused.
- **A failed weekly release on the lab's builder VM is reported as failed.** The guest's
  runner was started through actions/runner's `run.sh`, which turns the listener's
  `100 + the job's result` into 0, so `builder.sh weekly` read every job as passed. The
  listener now starts directly, as the CI guests of `shidashi ci` already did. The tests'
  stand-in runner had passed the code through `run.sh`, which is how this went unseen.
- **A dev VM no longer leaves its work disk behind.** bentoolkit names every build VM
  afresh, so each stopped session kept a disk full of builds. `vm stop` now removes a
  `--dev` session's directory, and `vm start --dev` removes the dev sessions whose VM died
  without a stop. Only sessions marked dev are touched: a plain session's `--disk` is kept.
- **`vm run` stopped by its timeout or Ctrl+C now stops the command in the guest too.**
  ssh runs no terminal there, so killing it left the guest command -- a whole build --
  running; the shell sshd starts now records its process group, and a second ssh kills it.
  A command past its timeout exits 124 in both modes (it was -1 without `--stream`).
- **ISOs record their pins.** `bentoo/build.json` gains a `pins` block (the `::gentoo`
  snapshot and the overlay commits); older ISOs are read through the commit that built
  them.

### Security

- **`::gentoo` re-pinned to 20261101, `::bentoo` to the same day.** 20260928 lacks the
  fixes of GLSA 202610-01 (ca-certificates trusted a compromised intermediate CA) and
  GLSA 202610-02 (php); every image built from it shipped both.
- **Toolchain bumped ahead of the 7-day cooldown, at the author's request.** pydantic
  2.14.3, pydantic-core 2.50.4, ruff 0.17.4, typer 0.28.0, typing-inspection 0.4.5 and
  mypy-extensions 1.2.0 through per-package waivers in `pyproject.toml`, dropped on
  2026-11-15 once every waived release was 7 days old (no version changed); uv 0.13.7 in
  CI; the CI guest's GitHub Actions runner to 2.338.2.

## [0.3.0] - 2026-10-22

A release becomes one command: the binhost knows its final binpkgs, and a dated set of
ISOs is built, published and signed with the maintainer's key.

### Added

- **The binhost knows its finals and can be published, signed.** A generation's PKGDIR
  only grew, and nothing told a flavor's final binpkg from a transient a cycle cut built
  on the way. Each shipped stage now records, in `.shidashi-pool.json` of its PKGDIR, the
  instances it installed (finals) and the ones its cuts produced (transients).
  `shidashi binhost show <arch>` prints every instance with its kind (final, transient,
  old, unrecorded). `shidashi binhost gc <arch>` moves the versions no pinned repository
  still carries, beyond `--keep` (default 2) per package, to
  `<cache>/quarantine/binpkgs/`, under the arch's owner lock, and reindexes; nothing is
  deleted, `--dry-run` shows the plan, and an image's `install` checkpoint still
  restores afterwards. A worker's push and pull honour the quarantine, so a gc on one
  side is not undone by the other. `shidashi binhost publish <arch> --key FPR` copies the
  recorded finals, and no other instance, to `<cache>/publish/<arch>/<generation>`, signs
  each gpkg and indexes them, replacing the previous pool atomically and leaving the
  build pool untouched; `shidashi binhost verify <arch> --pubkey FILE` checks every
  signature and the signed Manifest. Publishing a worker-owned arch is not covered yet.
- **One command makes a signed, dated release.** `shidashi release` was a stub, so a release
  meant running each build, publishing each binhost and signing by hand. `sudo shidashi
  release v3 systemd --key FPR` (or `--all`) now runs one `shidashi build --images all` per
  variant into `<cache>/releases/<YYYY.MM.DD>`, publishes each arch that built (a
  worker-owned arch is skipped and named in the summary), writes
  `release.json` (pins, commit, how each variant ended, its generation and toolchain) and
  signs every `<iso>.DIGESTS` and `SHA256SUMS` with the maintainer's key, through the gpg-agent
  of the user who ran `sudo`; `SHA256SUMS.asc` appears only once every signature verified. A
  failed variant does not stop the others, the summary names a toolchain refusal's successor
  PKGDIR and a newer upstream stage3 (reported, never adopted), and the exit code is 0 only
  when everything built, published and verified. `shidashi build --outcome-file` records how a
  build ended, and a toolchain refusal now prints the successor PKGDIR on its own line.

## [0.2.0] - 2026-10-17

A spare machine becomes a build oven: a worker boots paired, builds an arch and can keep
its cache.

### Added

- **An arch's cache can stay on its worker.** Every factory job used to copy the arch's
  binpkgs and fork points back to the host, about 90 GB for v3, so the host held a full copy
  even when only the worker built that arch. `shidashi worker cache-owner bentoo-lab --arch v3`
  now keeps v3's cache on bentoo-lab: a push sends only the pinned inputs and a job's pull
  brings back only its log, runs and ISOs. Host `factory`, `assemble` and `build --local` of v3
  are refused, naming the worker, and `build` sends v3's factory and every image to it.
  `--off` undoes it. Nothing is deleted on either side.
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
- **The worker ISO is a lean build oven: 656 MiB instead of 2 GiB (story 021).** An
  image whose `system.yaml` declares `slim:` gets a `slim` assemble step after the
  shared packages checkpoint: its world becomes the keep list and `emerge --depclean`
  removes the rest, kernel-module groups it never loads are dropped (graphics, media,
  sound, InfiniBand, Bluetooth…) and `depmod` re-run, firmware no remaining module
  asks for is deleted (patterns, symlink chains resolved inside the image, the
  firmware of built-in drivers, iwlwifi's older APIs), documentation, headers,
  sources, the compiler's internals and static libraries are pruned, and every
  command the worker runs is checked present, or the assemble fails naming them. The
  squashfs has a budget (the worker's: 600 MiB; it measured 521) and a larger one
  fails before that compression's ISO is written. The worker keeps systemd, OpenSSH,
  Python 3.14, podman, the job runner's tools, NetworkManager, dracut and lvm2, plus
  a rescue kit. Other images are assembled as before. Checked by an assemble in the
  builder VM and BIOS and UEFI boots with a work disk and a native job.
- **`worker provision N --wifi SSID`: a worker on Wi-Fi joins its network at boot
  (story 021).** The passphrase is asked twice without echo on a terminal or read as
  one line of stdin, and is stored only as a NetworkManager connection in the
  identity (0600; never printed, logged, registered or audited). The worker installs
  it root-only before NetworkManager starts; a malformed one is skipped and the wired
  identity still restores.
- **`shidashi vm start --forward HOST:GUEST` (story 021).** A guest TCP port becomes
  reachable on the host's `127.0.0.1:HOST` (repeatable); a host port already in use
  is refused before QEMU starts. Each `--disk` now carries a serial, so `worker
  disk-init` can prepare a VM worker's disk.

### Changed

- **ccache survives from one build to the next.** It hit 10.85% of 466k
  compiles, for two reasons, both reproduced with ccache 4.14. It identified gcc
  by its mtime, and the bootstrap rebuilds gcc on every fresh stage3, so each
  build discarded the previous one's cache; `CCACHE_COMPILERCHECK="%compiler%
  -dumpfullversion"` identifies it by release (16.2.1), so a weekly gcc snapshot
  keeps the cache and a new release starts a fresh one. And the build directory carries the package
  version, so a version bump missed even for unchanged files;
  `CCACHE_BASEDIR="/var/tmp/portage"` makes those paths relative. Each stage's
  `emerge-stage` audit step now records the cache's hits, misses and hit rate
  for that emerge. Measured on bentoo-lab (v3, 2026-10-17): a second from-source
  build of the base stage, with gcc rebuilt by the bootstrap, hit 99.6% of 48k
  compiles (11.6% with an empty cache), and its emerge took 312 min instead of
  378 (-17%). The wall clock gains less than the hit rate suggests: llvm, gcc and
  glibc stay outside ccache, and configure and install steps are not cached.
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

### Removed

- **sccache is no longer wired into the build.** It never ran: `package.env/sccache`
  listed no package and its cache stayed empty. Turned on, it would have broken the
  build -- its TCP port is unreachable under the nspawn network sandbox, the
  sandbox had no write access to its cache and the cache was owned by root. Only
  about 17 packages compile Rust from source, and sccache itself would have had to
  be built in every bootstrap. `env/sccache.conf`, `package.env/sccache`, the
  stats hook in the base `bashrc` (its only content) and the container bind are
  gone; `dev-util/sccache` stays in `@devel` for the user.

### Fixed

- **`shidashi doctor` reported `gcc ok` on a host whose gcc cannot name its CPU, and
  the factory's ISA check was then skipped in silence (story 023).** The slim worker
  keeps the gcc driver but prunes `cc1`, so `gcc -march=native` names nothing. The
  `gcc` line now runs that probe and is ok only with the arch it names (`names this
  CPU: znver5`); otherwise it is an optional failure saying the ISA check is skipped.
  The factory's `isa-check` step records `skipped: host CPU unknown` instead of
  reading like a clean scan.
- **The README placed the Wi-Fi passphrase only on the medium (story 023).** The host
  keeps a copy of the identity too, under
  `~/.local/share/shidashi/worker/<name>/identity/wifi/` (0600 in a 0700 directory).
- **Stale Python bytecode could ship in an image (story 021).** A layer's `rootfs/`
  was copied as it stood in the working tree, `__pycache__/*.pyc` included; the copy
  now skips them.
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
- **`worker provision` and `worker status` said too little (story 022).** A leftover
  `~/.local/share/shidashi/worker/<name>/` made `provision` generate a key and write the
  whole ISO copy before failing with "Directory not empty"; it is now refused up front,
  writing nothing, naming the directory and `--replace`. `worker status` with no name
  listed a provisioned worker it never reached as refused and exited 1; it now reads
  `not reached yet: run shidashi worker status <name>`, probes nothing for it, and that
  line alone does not fail the listing.
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

[Unreleased]: https://github.com/obentoo/shidashi/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/obentoo/shidashi/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/obentoo/shidashi/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/obentoo/shidashi/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/obentoo/shidashi/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/obentoo/shidashi/releases/tag/v0.1.0
