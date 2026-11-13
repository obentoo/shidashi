"""The guest side of a dev VM: one script that turns the live medium into a build host.

The live medium is built to be installed, not to build on, and a manual test
(2026-11-13, minimal 2026.11.12) found what a build is missing:

- ``/var/db/repos/{gentoo,bentoo}`` ship empty, so the profile points nowhere: each
  pinned tree is mounted read-only from its virtiofs share (``repo-<name>``);
- the live root is a ~1.6 GB tmpfs: the work disk (``/dev/vda``, new on every start) is
  formatted and holds ``/var/tmp``, ``DISTDIR``, ``PKGDIR``, the binhost index cache and
  ``/var/log/portage``;
- the medium's ``binrepos.conf`` is the stage3's -- Gentoo's generic x86-64 binhost.

It also makes ``/var/db/repos/staged``, the repository bentoolkit extracts a package
directory into, with a priority above the pinned repositories.

Binhosts, in the order the author chose: the host's own pool for the ISO's generation
(``local_pool``, shared read-only, no signature: it never left the host) at the highest
priority, then the medium's binhost when it answers. With neither, an EMPTY local pool:
measured on the guest, ``emerge --getbinpkg`` with no binrepos still builds from source but
prints a ``!!!`` line, which a reader of the build log would chase for nothing.

The script prints one ``shidashi-dev: binhost <name> <uri>`` line per binhost in use.
``SHIDASHI_GUEST_ROOT`` prefixes every path it writes -- empty on a guest, a temporary
directory in the tests, which run it for real with the root-only commands stubbed.
"""

import re
import shlex
from collections.abc import Sequence

#: The repository bentoolkit ships a package into, and where it lives in the guest.
STAGED_REPO = "bentoo-staged"
STAGED_DIR = "/var/db/repos/staged"
#: Above the pinned repositories' default 0: the staged copy of a package is the one built.
STAGED_PRIORITY = 100
#: The virtiofs tag and mount point of the host's binary package pool.
POOL_TAG = "binpool"
POOL_MOUNT = "/mnt/binpool"
#: Where the medium's own binrepos files go when their binhost does not answer.
ISO_BINREPOS_ASIDE = "etc/portage/binrepos.conf.aside"
_RESULT = "shidashi-dev: binhost"
_WORK = "/mnt/work"
_REPO_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
#: One HEAD of the binhost's index, from the guest: what "available" means.
_PROBE = (
    "import sys, urllib.request as u; "
    "u.urlopen(u.Request(sys.argv[1].rstrip('/') + '/Packages', method='HEAD'), timeout=15)"
)


def setup_script(repos: Sequence[str], *, local_pool: bool) -> str:
    """The bash script that readies the guest; ``repos`` are the pinned repositories,
    ``gentoo`` first, each shared as ``repo-<name>``. Pure."""
    if not repos or repos[0] != "gentoo":
        raise ValueError(f"the first repository must be gentoo, got {list(repos)}")
    for name in repos:
        if not _REPO_NAME.fullmatch(name):
            raise ValueError(f"repository name {name!r} is not [a-z0-9_-]+")
    mounts = "\n".join(
        f'mkdir -p "$R/var/db/repos/{n}"\nmount -t virtiofs -o ro repo-{n} "$R/var/db/repos/{n}"'
        for n in repos
    )
    pool = (
        f'mkdir -p "$R{POOL_MOUNT}"\n'
        f'mount -t virtiofs -o ro {POOL_TAG} "$R{POOL_MOUNT}"\n'
        f'printf "[bentoo-local]\\nsync-uri = file://{POOL_MOUNT}\\npriority = 20\\n'
        f'verify-signature = false\\n" > "$R/etc/portage/binrepos.conf/bentoo-local.conf"\n'
        f'echo "{_RESULT} bentoo-local file://{POOL_MOUNT}"'
        if local_pool
        else ""
    )
    return f"""set -eu
R="${{SHIDASHI_GUEST_ROOT:-}}"
W="$R{_WORK}"
mkfs.ext4 -q -F /dev/vda
mkdir -p "$W"
mount /dev/vda "$W"
mkdir -p "$W/tmp" "$W/distfiles" "$W/binpkgs" "$W/binhost" "$W/logs" "$W/empty-pool"
chmod 1777 "$W/tmp"
chown portage:portage "$W/distfiles" "$W/binpkgs" "$W/logs"
{mounts}
bind() {{ mkdir -p "$R$2"; mount --bind "$W/$1" "$R$2"; }}
bind tmp /var/tmp
bind distfiles "$(portageq envvar DISTDIR)"
bind binpkgs "$(portageq envvar PKGDIR)"
bind binhost /var/cache/binhost
bind logs /var/log/portage
mkdir -p "$W/staged/profiles" "$W/staged/metadata"
echo {STAGED_REPO} > "$W/staged/profiles/repo_name"
printf "masters = {" ".join(repos)}\\nthin-manifests = true\\nsign-manifests = false\\n" \\
  > "$W/staged/metadata/layout.conf"
bind staged {STAGED_DIR}
printf "[{STAGED_REPO}]\\nlocation = {STAGED_DIR}\\npriority = {STAGED_PRIORITY}\\n" \\
  > "$R/etc/portage/repos.conf/zz-{STAGED_REPO}.conf"
mapfile -t iso_confs < <(find "$R/etc/portage/binrepos.conf" -mindepth 1 -maxdepth 1 \\
  -type f -name '*.conf' | sort)
{pool}
for conf in "${{iso_confs[@]}}"; do
  name=$(sed -n 's/^\\[\\(.*\\)\\]$/\\1/p' "$conf" | head -n 1)
  uri=$(sed -n 's/^[[:space:]]*sync-uri[[:space:]]*=[[:space:]]*//p' "$conf" | head -n 1)
  if python3 -c {shlex.quote(_PROBE)} "$uri" >/dev/null 2>&1; then
    echo "{_RESULT} $name $uri"
  else
    mkdir -p "$R/{ISO_BINREPOS_ASIDE}"
    mv "$conf" "$R/{ISO_BINREPOS_ASIDE}/"
  fi
done
if [ -z "$(find "$R/etc/portage/binrepos.conf" -mindepth 1 -maxdepth 1 -type f \\
  -name '*.conf')" ]; then
  PKGDIR="$W/empty-pool" emaint binhost --fix >/dev/null
  printf "[none]\\nsync-uri = file://{_WORK}/empty-pool\\nverify-signature = false\\n" \\
    > "$R/etc/portage/binrepos.conf/none.conf"
  echo "{_RESULT} none file://{_WORK}/empty-pool"
fi
"""


def parse_binhosts(stdout: str) -> list[dict[str, str]]:
    """The binhosts the script reported, in its order. Pure."""
    found = []
    for line in stdout.splitlines():
        if line.startswith(_RESULT + " "):
            name, _, uri = line.removeprefix(_RESULT + " ").partition(" ")
            found.append({"name": name, "uri": uri})
    return found
