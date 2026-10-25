#!/bin/bash
# guest-runner.sh — run ONE just-in-time GitHub Actions runner in the builder guest.
#
# Runs as the guest's root, started by `builder.sh weekly` as the transient unit
# shidashi-runner. The host hands the JIT config over ssh stdin into /run/shidashi/jitconfig
# (tmpfs, 0600). A JIT runner never registers with config.sh: it takes that config, runs
# one job and exits, so nothing stays registered on GitHub.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck disable=SC1091  # runner.env is plain KEY=value data, nothing to follow
. "$REPO/lab/vm/runner.env"
JITCONFIG=/run/shidashi/jitconfig
# the work disk: the job's checkout and venv live beside the runner, too big for tmpfs
WORK=/mnt/work/runner/$RUNNER_VERSION

die() { echo "guest-runner: $*" >&2; exit 1; }
trap 'rm -f "$JITCONFIG"' EXIT

[[ ${RUNNER_VERSION:-} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "RUNNER_VERSION in runner.env is not x.y.z"
[[ ${RUNNER_SHA256:-} =~ ^[0-9a-f]{64}$ ]] || die "RUNNER_SHA256 in runner.env is not a 64-digit hex digest"
[ -f "$JITCONFIG" ] && [ ! -L "$JITCONFIG" ] || die "$JITCONFIG is missing"
[ "$(stat -c '%a %u' "$JITCONFIG")" = "600 0" ] || die "$JITCONFIG must be mode 0600 and root's"

echo "guest-runner: actions/runner $RUNNER_VERSION"
tarball=$WORK/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz
rm -rf "$WORK"
mkdir -p "$WORK/dist"
curl -fsSL --retry 3 -o "$tarball" \
  "https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz"

actual=$(sha256sum "$tarball" | cut -d' ' -f1)
if [ "$actual" != "$RUNNER_SHA256" ]; then
  rm -f "$tarball"
  die "sha256 of the runner tarball is $actual, runner.env pins $RUNNER_SHA256; not running it"
fi
tar -xzf "$tarball" -C "$WORK/dist"
cd "$WORK/dist"

# A JIT config never touches a shared directory; it leaves tmpfs only as this argument. The
# file goes first: after `exec` an EXIT trap would never fire.
cfg=$(<"$JITCONFIG")
rm -f "$JITCONFIG"
export RUNNER_ALLOW_RUNASROOT=1
# without it the runner exits 0 whatever the job's result (actions/runner Runner.cs)
export ACTIONS_RUNNER_RETURN_JOB_RESULT_FOR_HOSTED=true
export ACTIONS_RUNNER_HOOK_JOB_STARTED="$REPO/lab/vm/runner-hook.sh"
exec ./run.sh --jitconfig "$cfg"
