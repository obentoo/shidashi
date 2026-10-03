#!/bin/bash
# guest-setup.sh — prepare the builder guest (idempotent). Runs as the guest's root;
# lab/vm/builder.sh setup calls it after mounting the repository.
#
#   /mnt/lab     the host's cache, read-only (virtiofs tag "lab")
#   /mnt/work    the guest's own disk (/dev/vda, btrfs): scratch and the overlay's upper layer
#   /work/cache  overlay: the host's cache below, the guest's writes above -- new binpkgs,
#                fork points and distfiles land on the guest's disk, never on the host
#   /mnt/out     writable share: job logs, audit trails (runs/) and ISOs (iso/)
set -euo pipefail

mounted() { findmnt -rn -M "$1" >/dev/null; }

for tag in lab out; do
  mkdir -p "/mnt/$tag"
  mounted "/mnt/$tag" || mount -t virtiofs "$tag" "/mnt/$tag"
done

blkid /dev/vda >/dev/null 2>&1 || mkfs.btrfs -q -L shidashi-work /dev/vda
mkdir -p /mnt/work
mounted /mnt/work || mount -o noatime,compress=zstd:1 /dev/vda /mnt/work
mkdir -p /mnt/work/upper /mnt/work/ovl /mnt/work/scratch /work/cache
mounted /work/cache || mount -t overlay overlay \
  -o lowerdir=/mnt/lab,upperdir=/mnt/work/upper,workdir=/mnt/work/ovl /work/cache

for m in /mnt/lab /mnt/out /mnt/work /work/cache; do findmnt -rn -o TARGET,FSTYPE -M "$m"; done
echo "guest-setup: ready"
