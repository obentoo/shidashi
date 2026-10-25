#!/bin/bash
# runner-hook.sh — ACTIONS_RUNNER_HOOK_JOB_STARTED: the runner runs it before any step of a
# job, and a non-zero exit fails the job. This runner may start only the weekly release,
# dispatched on main of obentoo/shidashi; anything else (a fork's pull request, another
# workflow, another branch) is refused before it can run a line on this machine.
#
# Every value is compared whole -- a prefix or a pattern would admit `...@refs/heads/main-evil`.
set -euo pipefail

refuse() { printf 'runner-hook: refusing the job: %s is %q, not %q\n' "$1" "${!1-}" "$2" >&2; exit 1; }
expect() { [ "${!1-}" = "$2" ] || refuse "$1" "$2"; }

expect GITHUB_REPOSITORY obentoo/shidashi
expect GITHUB_EVENT_NAME workflow_dispatch
expect GITHUB_REF refs/heads/main
expect GITHUB_WORKFLOW_REF obentoo/shidashi/.github/workflows/weekly-release.yml@refs/heads/main
