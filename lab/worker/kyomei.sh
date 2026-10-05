#!/bin/bash
# kyomei.sh [-n NAME] [ADDRESS] — the host's side of `shidashi kyomei` (pairing a worker).
#
# Serves ONLY the worker key's public half over HTTP on ADDRESS:8765 (default: the
# address of the default route) for a limited time, then stops; with -n, also the name
# the worker takes (its hostname). The worker's console runs `shidashi kyomei <this
# host's IP>`, allows the key for root, takes the name and starts sshd.
# Compare the fingerprint printed here with the one the worker prints; after that,
# reach it with:
#
#   ssh -i ~/.local/share/shidashi/worker/id_ed25519 root@<worker IP>
#
# The key is created on first use, without a passphrase: the host drives the worker
# unattended. Runs as you; nothing here needs root.
#
#   KYOMEI_PORT (8765), KYOMEI_TIMEOUT (1800 s)
set -euo pipefail

name=
while getopts n: opt; do
  case $opt in
    n) name=$OPTARG ;;
    *) echo "usage: kyomei.sh [-n NAME] [ADDRESS]" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
# a hostname label (RFC 1123): what hostnamectl accepts as a static name
if [[ -n $name && ! $name =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
  echo "kyomei.sh: not a hostname: $name" >&2
  exit 2
fi

key=${XDG_DATA_HOME:-$HOME/.local/share}/shidashi/worker/id_ed25519
port=${KYOMEI_PORT:-8765}
timeout=${KYOMEI_TIMEOUT:-1800}
address=${1:-$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')}
[[ -n $address ]] || { echo "kyomei.sh: no address; pass one" >&2; exit 1; }

if [[ ! -f $key ]]; then
  install -d -m 700 "$(dirname "$key")"
  ssh-keygen -q -t ed25519 -N '' -C "shidashi-worker@$(hostname)" -f "$key"
fi

srv=$(mktemp -d)
trap 'rm -rf "$srv"' EXIT
cp "$key.pub" "$srv/id_ed25519.pub"
[[ -z $name ]] || printf '%s\n' "$name" > "$srv/hostname"

echo "kyomei: on the worker's console run:  shidashi kyomei $address"
[[ -z $name ]] || echo "kyomei: the worker will be named $name"
echo "kyomei: the worker must show this key:"
ssh-keygen -lf "$key.pub"
echo "kyomei: serving on $address:$port for ${timeout}s (Ctrl+C stops it)"
timeout "$timeout" python3 -m http.server "$port" --bind "$address" --directory "$srv" || true
