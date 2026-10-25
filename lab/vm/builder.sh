#!/bin/bash
# builder.sh — the Bentoo builder VM: a live Bentoo ISO in which builds run as the
# guest's root, so the host never needs sudo for factory or assemble.
#
#   lab/vm/builder.sh start [ISO]        boot it (default ISO: the newest minimal one)
#   lab/vm/builder.sh setup              mount the shares, the work disk and the overlay
#   lab/vm/builder.sh job NAME ARGS...   run `shidashi ARGS...` in the guest, detached
#   lab/vm/builder.sh status             running units and finished jobs
#   lab/vm/builder.sh stop
#   lab/vm/builder.sh weekly             one unsigned release on an ephemeral GitHub runner
#                                        in the VM (boots it, stops it, prunes the releases)
#   lab/vm/builder.sh timer-install      run `weekly` every Sunday 01:00 UTC (user timer)
#
# Layout (each overridable through the environment):
#   SHIDASHI_CACHE   /var/cache/shidashi        the host's cache, shared READ-ONLY: the
#                                               guest's writes go to an overlay on its disk
#   SHIDASHI_VM_DIR  /var/lib/shidashi/vm       per VM: disk.qcow2 (sparse, grows) and out/
#                                               (jobs/, runs/, iso/ -- written by the guest,
#                                               owned by you on the host)
#   VM_CPUS, VM_MEMORY, VM_DISK_SIZE             24, 32G, 200G
#
# Runs as you, not root: the kvm group opens /dev/kvm and /dev/vhost-vsock. The VM is
# QEMU started by `shidashi vm`, not a libvirt domain.
#
# weekly: SHIDASHI_GH_TOKEN_FILE (default ~/.config/shidashi/github-token, mode 0600) holds the
# token `shidashi ci` uses; WEEKLY_TIMEOUT (seconds, default 172800) bounds the wait for the
# runner; lab/vm/runner.env pins the runner. The JIT runner config travels over ssh stdin
# into the guest's tmpfs only -- never an argument, never a shared directory.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
NAME=builder
CID=62
CACHE=${SHIDASHI_CACHE:-/var/cache/shidashi}
STATE=${SHIDASHI_VM_DIR:-/var/lib/shidashi/vm}/$NAME
OUT=$STATE/out

shidashi() { "$REPO/.venv/bin/python" -c 'from shidashi.cli import app; app()' "$@"; }
vm_run() { shidashi vm run --name "$NAME" --work-dir "$STATE/host" --timeout "${TIMEOUT:-600}" "$@"; }

start_vm() {
  local iso=${1:-}
  if [ -z "$iso" ]; then
    iso=$(find "$OUT/iso" -maxdepth 1 -name 'bentoo-*-minimal-*.iso' \
            -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
  fi
  [ -n "$iso" ] && [ -f "$iso" ] || { echo "no minimal ISO found; give one: $0 start ISO"; return 1; }
  mkdir -p "$OUT/jobs" "$OUT/runs" "$OUT/iso"
  shidashi vm start "$iso" --name "$NAME" --cid "$CID" \
    --cpus "${VM_CPUS:-24}" --memory "${VM_MEMORY:-32G}" \
    --disk "$STATE/disk.qcow2" --disk-size "${VM_DISK_SIZE:-200G}" \
    --share "repo=$REPO" --share "lab=$CACHE" --share "out=$OUT:rw" \
    --work-dir "$STATE/host"
}

setup_guest() {
  TIMEOUT=300 vm_run "mkdir -p '$REPO' && { findmnt -rn -M '$REPO' >/dev/null || mount -t virtiofs repo '$REPO'; } && bash '$REPO/lab/vm/guest-setup.sh'"
}

stop_vm() { shidashi vm stop --name "$NAME" --work-dir "$STATE/host"; }

# --- weekly ---------------------------------------------------------------------------------

RUNNER_UNIT=shidashi-runner
WEEKLY_BOOTED=0   # this run booted the VM
WEEKLY_OWNS=0     # the runner unit is started: a failure from here on must power the VM off

weekly_abort() {
  echo "weekly: $*" >&2
  if [ "$WEEKLY_BOOTED" = 1 ] || [ "$WEEKLY_OWNS" = 1 ]; then
    stop_vm || echo "weekly: cannot stop the builder VM" >&2
  fi
  exit 1
}

weekly() {
  exec </dev/null   # only the config handoff below reads a pipe; no vm_run may eat our stdin
  local token_file=${SHIDASHI_GH_TOKEN_FILE:-$HOME/.config/shidashi/github-token}
  local timeout=${WEEKLY_TIMEOUT:-172800} poll=${WEEKLY_POLL:-60}
  local cfg out load active sub code bad=0 deadline rc

  # shellcheck disable=SC1091  # runner.env is plain KEY=value data, nothing to follow
  . "$REPO/lab/vm/runner.env"
  echo "weekly: actions/runner $RUNNER_VERSION, runner group $RUNNER_GROUP_ID"
  mkdir -p "$OUT/releases"

  if ! vm_run true >/dev/null 2>&1; then
    WEEKLY_BOOTED=1
    start_vm || weekly_abort "cannot boot the builder VM"
  fi
  setup_guest || weekly_abort "cannot prepare the builder guest"

  trap 'weekly_abort "interrupted"' TERM INT HUP
  { set +x; } 2>/dev/null   # the config is a credential: never traced, never echoed
  cfg=$(shidashi ci jit-config --repo obentoo/shidashi --token-file "$token_file" \
          --runner-group-id "$RUNNER_GROUP_ID" --label bentoo-builder \
          --name "builder-$(date -u +%Y%m%dT%H%M%SZ)") || weekly_abort "cannot mint the JIT runner config"
  [ -n "$cfg" ] || weekly_abort "GitHub returned an empty runner config"
  printf '%s' "$cfg" | vm_run 'install -d -m 0700 /run/shidashi && install -m 0600 /dev/stdin /run/shidashi/jitconfig' \
    || weekly_abort "cannot hand the runner config to the guest"
  cfg=

  WEEKLY_OWNS=1
  vm_run "systemd-run --unit=$RUNNER_UNIT --quiet -p RemainAfterExit=yes --working-directory='$REPO' --setenv=HOME=/root --setenv=SHIDASHI_CACHE=/work/cache --setenv=SHIDASHI_SCRATCH=/mnt/work/scratch --setenv=SHIDASHI_RUNS=/mnt/out/runs bash '$REPO/lab/vm/guest-runner.sh'" \
    || weekly_abort "cannot start the runner unit $RUNNER_UNIT in the guest"
  shidashi ci dispatch --repo obentoo/shidashi --token-file "$token_file" \
    --workflow weekly-release.yml --ref main \
    || weekly_abort "cannot dispatch weekly-release.yml"

  deadline=$((SECONDS + timeout))
  while :; do
    if out=$(vm_run "systemctl show $RUNNER_UNIT -p LoadState -p ActiveState -p SubState -p ExecMainStatus"); then
      bad=0
      load='' active='' sub='' code=''
      while IFS='=' read -r key value; do
        case $key in
          LoadState) load=$value ;;
          ActiveState) active=$value ;;
          SubState) sub=$value ;;
          ExecMainStatus) code=$value ;;
        esac
      done <<<"$out"
      [ "$load" != not-found ] || weekly_abort "the runner unit is gone: its exit status is lost"
      # RemainAfterExit keeps a finished unit: "exited" when it passed, "failed" when it did not
      if [ "$active" = inactive ] || [ "$active" = failed ] || [ "$sub" = exited ]; then
        break
      fi
    else
      bad=$((bad + 1))
      [ "$bad" -lt 5 ] || weekly_abort "cannot read the runner status"
    fi
    [ "$SECONDS" -lt "$deadline" ] || weekly_abort "the runner did not exit within $timeout s"
    sleep "$poll"
  done
  [[ $code =~ ^[0-9]+$ ]] || weekly_abort "the runner unit reports no exit status ($code)"

  rc=$code
  # with ACTIONS_RUNNER_RETURN_JOB_RESULT_FOR_HOSTED the runner exits 100 + the job result
  # (guest-runner.sh sets it): 100 is a passed job, 102 a failed one
  [ "$rc" != 100 ] || rc=0
  echo "weekly: the runner exited $rc"
  if ! stop_vm; then
    echo "weekly: cannot stop the builder VM" >&2
    rc=1
  fi
  if ! shidashi release prune --keep 4 --purge-pruned --output-root "$OUT/releases"; then
    echo "weekly: release prune failed" >&2
    rc=1
  fi
  exit "$rc"
}

# --- timer-install --------------------------------------------------------------------------

timer_install() {
  local units=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user name text
  case $REPO in
    *[[:space:]%\$\\\"\'\|\&]*) echo "timer-install: the checkout path $REPO has characters systemd would expand"; return 1 ;;
  esac
  mkdir -p "$units" || { echo "timer-install: cannot create $units"; return 1; }
  for name in shidashi-weekly.service shidashi-weekly.timer; do
    text=$(<"$REPO/lab/systemd/$name") || { echo "timer-install: cannot read $name"; return 1; }
    printf '%s\n' "${text//@REPO@/"$REPO"}" >"$units/$name" || { echo "timer-install: cannot write $units/$name"; return 1; }
  done
  systemctl --user daemon-reload || { echo "timer-install: systemctl --user daemon-reload failed"; return 1; }
  systemctl --user enable --now shidashi-weekly.timer || { echo "timer-install: cannot enable shidashi-weekly.timer"; return 1; }
  echo "timer-install: shidashi-weekly.timer runs Sunday 01:00 UTC; it fires while you are logged out only after: loginctl enable-linger $(id -un)"
}

cmd=${1:-}
shift || true
case "$cmd" in
  start) start_vm "$@" ;;
  setup) setup_guest ;;
  weekly) weekly ;;
  timer-install) timer_install ;;
  job)
    [ $# -ge 2 ] || { echo "usage: $0 job NAME SHIDASHI-ARGS..."; exit 2; }
    printf -v args '%q ' "$@"
    vm_run "bash '$REPO/lab/vm/guest-job.sh' $args"
    echo "log: $OUT/jobs/$1.log  (exit code in $OUT/jobs/$1.rc when it ends)"
    ;;
  status)
    vm_run "systemctl list-units --no-legend 'shidashi-*' || true"
    for rc in "$OUT"/jobs/*.rc; do [ -e "$rc" ] && echo "$(basename "$rc" .rc): rc=$(cat "$rc")"; done
    ;;
  stop)
    shidashi vm stop --name "$NAME" --work-dir "$STATE/host"
    ;;
  *)
    sed -n '2,31p' "$0"; exit 2 ;;
esac
