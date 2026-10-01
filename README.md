# Shidashi 仕出し

> The catering of **bentoo** — prepares and serves builds and installation ISOs from a Gentoo stage3.

Shidashi always starts from an **official stage3** and applies the bentoo layer (config + packages),
compiling in isolated environments per *flavor*, serving binpkgs in USE variations via a
binhost, and assembling live ISOs — across multiple architectures and flavors, with weekly
releases (**every Sunday at 00:00**).

📄 Full architecture: **[OVERVIEW.md](OVERVIEW.md)**.

## Status

**Phases 0 and 1 complete (code).** The Python package `shidashi/` (14 modules), the recipes
(`variants/`) and both subsystems (Package Factory + ISO Assembler) **are implemented and
covered by tests** (381 passing). The live ISO boot was validated on QEMU/KVM; validation of the
**build on a root Gentoo host** is *host-gated* and remains deferred by design (pilots in progress).
Phases 2–5 open — see the roadmap in OVERVIEW.md §17 and the schedule in `.epic/docs/ROADMAP.md`.

The commands below already run off-host (`recipe`, `pretend`); `factory`/`assemble` require a root host.

## Requirements

- **Gentoo host** (Shidashi uses Portage's Python API: `import portage`).
- **Python ≥ 3.14**.
- Planned distribution: **ebuild** `app-misc/shidashi` in the bentoo overlay.

## Installation (dev)

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

## Usage

```sh
shidashi recipe show v3 minimal systemd   # shows the resolved recipe (axis deep-merge)
shidashi recipe validate v3 kde systemd   # validates the fragment merge
shidashi factory v3 kde systemd           # compiles binpkgs → binhost
shidashi assemble v3 kde systemd          # assembles the ISO from the binhost
shidashi release --all                    # orchestrates the whole matrix
```

> Initial pilot: `v3 × minimal × systemd`, then `v3 × kde × systemd`.

## Model

- **Composable axes:** `arch × flavor × init` (see `variants/`, co-located per axis).
- **Flavors:** `minimal` (TTY only) · `kde` (Qt) · `gnome` (GTK) · `wm` (Wayland-only: Hyprland/Sway/niri).
- **Archs:** `v3` (baseline) · `znver5` (Zen 5, Tier 1) · `arrowlake` (Tier 2, build-only).
- **Two subsystems:** Package Factory (compiles) + ISO Assembler (assembles).
- **Build:** persistent trunk (weekly delta) + full clean wipe on *toolchain-bump*.
- **Input determinism:** `::gentoo` snapshot pin per release.
- **Language:** Python ≥ 3.14 — because Portage *is* a Python library.

## License

MIT — see [LICENSE](LICENSE).
