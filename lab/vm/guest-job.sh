#!/bin/bash
# guest-job.sh NAME SHIDASHI-ARGS... — run one shidashi command detached from ssh.
#
# A transient systemd unit (shidashi-NAME) outlives the ssh session that started it.
# Output: /mnt/out/jobs/NAME.log; exit code: NAME.rc; audit trails: /mnt/out/runs --
# all visible on the host. Refuses to reuse a running unit.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
name=${1:?job name}
shift
[[ "$name" =~ ^[a-z0-9-]+$ ]] || { echo "job name: a-z, 0-9 and -"; exit 2; }
if systemctl is-active --quiet "shidashi-$name"; then echo "shidashi-$name is running"; exit 1; fi
rm -f "/mnt/out/jobs/$name.rc"
# the inner shell expands $1, $@ and $? -- not this one
# shellcheck disable=SC2016
systemd-run --unit="shidashi-$name" --collect --quiet \
  --working-directory="$REPO" \
  --setenv=SHIDASHI_CACHE=/work/cache \
  --setenv=SHIDASHI_SCRATCH=/mnt/work/scratch \
  --setenv=SHIDASHI_RUNS=/mnt/out/runs \
  /bin/bash -c 'job=$1; shift; "$@" > "/mnt/out/jobs/$job.log" 2>&1; echo $? > "/mnt/out/jobs/$job.rc"' \
  bash "$name" "$REPO/.venv/bin/python" -c "from shidashi.cli import app; app()" "$@"
echo "started shidashi-$name"
