#!/bin/bash
# builder.sh — the Bentoo builder VM: a live Bentoo ISO in which builds run as the
# guest's root, so the host never needs sudo for factory or assemble.
#
#   lab/vm/builder.sh start [ISO]        boot it (default ISO: the newest minimal one)
#   lab/vm/builder.sh setup              mount the shares, the work disk and the overlay
#   lab/vm/builder.sh job NAME ARGS...   run `shidashi ARGS...` in the guest, detached
#   lab/vm/builder.sh status             running units and finished jobs
#   lab/vm/builder.sh stop
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
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
NAME=builder
CID=62
CACHE=${SHIDASHI_CACHE:-/var/cache/shidashi}
STATE=${SHIDASHI_VM_DIR:-/var/lib/shidashi/vm}/$NAME
OUT=$STATE/out

shidashi() { "$REPO/.venv/bin/python" -c 'from shidashi.cli import app; app()' "$@"; }
vm_run() { shidashi vm run --name "$NAME" --work-dir "$STATE/host" --timeout "${TIMEOUT:-600}" "$@"; }

cmd=${1:-}
shift || true
case "$cmd" in
  start)
    iso=${1:-}
    if [ -z "$iso" ]; then
      iso=$(find "$OUT/iso" -maxdepth 1 -name 'bentoo-*-minimal-*.iso' \
              -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    fi
    [ -n "$iso" ] && [ -f "$iso" ] || { echo "no minimal ISO found; give one: $0 start ISO"; exit 1; }
    mkdir -p "$OUT/jobs" "$OUT/runs" "$OUT/iso"
    shidashi vm start "$iso" --name "$NAME" --cid "$CID" \
      --cpus "${VM_CPUS:-24}" --memory "${VM_MEMORY:-32G}" \
      --disk "$STATE/disk.qcow2" --disk-size "${VM_DISK_SIZE:-200G}" \
      --share "repo=$REPO" --share "lab=$CACHE" --share "out=$OUT:rw" \
      --work-dir "$STATE/host"
    ;;
  setup)
    TIMEOUT=300 vm_run "mkdir -p '$REPO' && { findmnt -rn -M '$REPO' >/dev/null || mount -t virtiofs repo '$REPO'; } && bash '$REPO/lab/vm/guest-setup.sh'"
    ;;
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
    sed -n '2,24p' "$0"; exit 2 ;;
esac
