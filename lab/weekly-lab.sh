#!/bin/bash
# weekly-lab.sh — one unsigned weekly release, built on the worker (bentoo-lab).
#
#   lab/weekly-lab.sh weekly          one cycle now
#   lab/weekly-lab.sh timer-install   run it every Sunday 01:00 UTC (user timer)
#
# This host only orchestrates: the arch's cache lives on the worker (`shidashi worker
# cache-owner`), so `shidashi release` sends the factory and every image there and brings
# the ISOs home. No GitHub runner, token or VM is involved, so nothing a pull request starts
# can reach the worker.
#
# Environment (each overridable):
#   WEEKLY_WORKER    bentoo-lab                      the worker that owns the arch's cache
#   WEEKLY_ARCH      v3
#   WEEKLY_INIT      systemd
#   WEEKLY_IMAGES    minimal,kde,gnome
#   WEEKLY_OUT       /var/cache/shidashi/releases    where the dated releases land
#   WEEKLY_KEEP      4                               releases kept here, and in .pruned/
#   WEEKLY_POWEROFF  0                               1: power the worker off after a green run
#   WEEKLY_ALLOW_DIRTY 0                             1: build HEAD although the tree has changes
#   SHIDASHI_SCRATCH /var/tmp/shidashi-<user>        job results; /var/tmp/shidashi is root's
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WORKER=${WEEKLY_WORKER:-bentoo-lab}
ARCH=${WEEKLY_ARCH:-v3}
INIT=${WEEKLY_INIT:-systemd}
IMAGES=${WEEKLY_IMAGES:-minimal,kde,gnome}
OUT=${WEEKLY_OUT:-/var/cache/shidashi/releases}
KEEP=${WEEKLY_KEEP:-4}
export SHIDASHI_SCRATCH=${SHIDASHI_SCRATCH:-/var/tmp/shidashi-$(id -un)}

shidashi() { "$REPO/.venv/bin/python" -c 'from shidashi.cli import app; app()' "$@"; }
say() { printf 'weekly-lab: %s\n' "$*"; }
die() { printf 'weekly-lab: %s\n' "$*" >&2; exit 1; }

weekly() {
  local out log dir rc=0

  mkdir -p "$SHIDASHI_SCRATCH" || die "cannot create $SHIDASHI_SCRATCH"
  out=$(shidashi worker status 2>&1) || die "cannot read the workers' status: $out"  # one line per worker
  grep -qE "^$WORKER +reachable( |$)" <<<"$out" || die "$WORKER is not reachable; nothing was built: $out"
  out=$(shidashi worker cache-owner "$WORKER" 2>&1) || die "cannot read what $WORKER owns: $out"
  grep -qE "(:|,) *$ARCH( |,|$)" <<<"$out" \
    || die "$WORKER does not own $ARCH's cache ($out); run: shidashi worker cache-owner $WORKER --arch $ARCH"

  say "building $ARCH/$INIT --images $IMAGES on $WORKER"
  local extra=()
  [ "${WEEKLY_ALLOW_DIRTY:-0}" != 1 ] || extra=(--allow-dirty)
  log=$(mktemp) || die "cannot create a temporary file"
  # expanded now: the trap fires after this function's locals are gone
  trap 'rm -f '"'$log'" EXIT
  set +e
  shidashi release "$ARCH" "$INIT" --unsigned --images "$IMAGES" --output-root "$OUT" "${extra[@]}" 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  set -e

  dir=$(sed -n 's/^release: //p' "$log" | tail -n 1)
  if [ -n "$dir" ]; then
    shidashi release report "$dir" || say "cannot print the report of $dir"
  else
    say "no release directory was reported"
    [ "$rc" -ne 0 ] || rc=1
  fi
  [ "$rc" -eq 0 ] || die "the release exited $rc; nothing pruned, so the last good releases stay"

  # only after a green run: a failed week must not push a good release out of the window
  shidashi release prune --keep "$KEEP" --purge-pruned --output-root "$OUT" || die "release prune failed"
  if [ "${WEEKLY_POWEROFF:-0}" = 1 ]; then
    shidashi worker poweroff "$WORKER" || say "cannot power $WORKER off"
  fi
  say "done: $dir"
}

timer_install() {
  local units=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user name text
  case $REPO in
    *[[:space:]%\$\\\"\'\|\&]*) die "the checkout path $REPO has characters systemd would expand" ;;
  esac
  mkdir -p "$units" || die "cannot create $units"
  for name in shidashi-weekly-lab.service shidashi-weekly-lab.timer; do
    text=$(<"$REPO/lab/systemd/$name") || die "cannot read $name"
    printf '%s\n' "${text//@REPO@/"$REPO"}" >"$units/$name" || die "cannot write $units/$name"
  done
  systemctl --user daemon-reload || die "systemctl --user daemon-reload failed"
  systemctl --user enable --now shidashi-weekly-lab.timer || die "cannot enable shidashi-weekly-lab.timer"
  say "shidashi-weekly-lab.timer runs Sunday 01:00 UTC; it fires while you are logged out only after: loginctl enable-linger $(id -un)"
}

case ${1:-} in
  weekly) weekly ;;
  timer-install) timer_install ;;
  *) sed -n '2,7p' "$0"; exit 2 ;;
esac
