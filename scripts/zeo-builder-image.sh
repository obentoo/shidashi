#!/usr/bin/env bash
# zeo-builder-image.sh — the container image Zeo's heavy work runs in on a build worker.
#
#     scripts/zeo-builder-image.sh [--worker NAME] [--dry-run]
#
# Zeo's `test.sh` (nextest over ~6 400 tests), clippy and its Xvfb proofs need a Gentoo
# build environment: the system Rust, the libraries app-editors/zeo builds against,
# Portage itself (`ebuild unpack` vendors the crates offline), rustup with wasm32-wasip2,
# Xvfb and dbus. This builds that image ON THE WORKER, into its podman storage, so the
# `lab` tool can run those jobs there instead of on the desktop.
#
# The trees are THIS MACHINE's, not Shidashi's pin (decided 2026-11-17): zeo-0.12 needs
# Rust >= 1.100.0, which only ::bentoo carries (since 2026-11-16), and today's ::bentoo
# does not load against the pinned ::gentoo (its rust.eclass inherits an llvm-r2.eclass
# the 2026-11-06 tree cannot provide). So the image mirrors what this desktop builds Zeo
# with: its ::gentoo (rsynced), ::bentoo at the overlay checkout's HEAD (committed
# content only), its package.use for app-editors/zeo. Once a pin carries both (allowed
# from 2026-11-23 by the 7-day rule), this should build from the pin instead.
#
# /var/cache/distfiles is the worker's SHARED distfiles cache (Shidashi's, mounted
# read-write so fetches land there once): the build never empties it.
#
# The base is the pin's `desktop` fork point (a full Bentoo desktop's USE and config),
# imported once as localhost/shidashi-desktop-base:<hash>; the toolbox is too minimal
# (-X) for Zeo's graphics stack. On top: `emerge --onlydeps` of the overlay's newest
# app-editors/zeo, dev-lang/rust-bin (eselected), cargo-nextest, rustup (its own
# toolchain with wasm32-wasip2 under /opt/rustup), Xvfb (xorg-server built `xvfb minimal`
# with no input or video drivers: a full X server pulls xorg-drivers, whose evdev wants
# mesa[X]; the proofs need only the virtual framebuffer), twm, dbus, fonts and rsync. Packages it compiles are kept as binpkgs under the worker's
# $WORK/builder/binpkgs, so a rebuild compiles only what changed.
#
# The build runs inside the worker's shared budget (contrib/worker/slot: every CPU,
# 24 GiB, waiting its turn). Its tag is localhost/shidashi-zeo-builder:<date>-<bentoo
# commit>; labels record the trees, the overlay commit, the Rust and the base. The two
# newest builder images stay; older ones are removed (not one a container uses).
#
# Options / environment:
#   --worker NAME        ssh destination of the worker (default: bentoo-lab)
#   --dry-run            print the plan and the Containerfile; touch nothing
#   ZEO_BUILDER_GENTOO   ::gentoo to mirror (default: portageq's gentoo, else
#                        /var/db/repos/gentoo)
#   ZEO_BUILDER_OVERLAY  the ::bentoo checkout (default: ../bentoo beside this repo)
#   ZEO_BUILDER_USE      package.use for app-editors/zeo (default:
#                        /etc/portage/package.use/zeo)
#   ZEO_BUILDER_RUST     the Rust version (default: this machine's rustc)
#   ZEO_BUILDER_LLVM_SLOT  LLVM_SLOT for the build (default: this machine's make.conf)
#   ZEO_BUILDER_BASE     the desktop fork point's file name on the worker (default: the
#                        newest *-desktop.tar there)
#
# Exit: 0 built (image and digest printed) · 1 the build failed · 2 usage or environment.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKER=bentoo-lab
DRY_RUN=0
WORK=/mnt/work
BUILDER="$WORK/builder"
FORK_POINTS="$WORK/cache/fork-points"

die() {
	printf 'zeo-builder-image: %s\n' "$2" >&2
	exit "$1"
}

while (($#)); do
	case "$1" in
	--worker)
		WORKER=${2:-}
		shift 2 || die 2 "--worker needs a name"
		;;
	--dry-run)
		DRY_RUN=1
		shift
		;;
	-h | --help)
		sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
		exit 0
		;;
	*) die 2 "unknown argument: $1" ;;
	esac
done

gentoo=${ZEO_BUILDER_GENTOO:-$(portageq get_repo_path / gentoo 2>/dev/null || echo /var/db/repos/gentoo)}
overlay=${ZEO_BUILDER_OVERLAY:-$(cd "$REPO_ROOT/.." && pwd)/bentoo}
use_file=${ZEO_BUILDER_USE:-/etc/portage/package.use/zeo}
rust=${ZEO_BUILDER_RUST:-$(rustc --version 2>/dev/null | awk '{print $2}')}
# this machine sets LLVM_SLOT globally in make.conf; zeo and rust-bin must agree on it
llvm_slot=${ZEO_BUILDER_LLVM_SLOT:-$(portageq envvar LLVM_SLOT 2>/dev/null || true)}

[[ -f $gentoo/metadata/timestamp.chk ]] || die 2 "no ::gentoo tree at $gentoo"
git -C "$overlay" rev-parse --verify -q HEAD >/dev/null || die 2 "no ::bentoo checkout at $overlay"
[[ -f $use_file ]] || die 2 "no package.use for app-editors/zeo at $use_file"
[[ $rust =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 2 "cannot tell the Rust version (rustc --version); set ZEO_BUILDER_RUST"
[[ $llvm_slot =~ ^[0-9]+$ ]] || die 2 "cannot tell LLVM_SLOT (portageq envvar LLVM_SLOT); set ZEO_BUILDER_LLVM_SLOT"

commit=$(git -C "$overlay" rev-parse HEAD)
short=${commit:0:12}
stamp=$(head -n 1 "$gentoo/metadata/timestamp.chk")
zeo_pf=$(git -C "$overlay" ls-tree --name-only HEAD app-editors/zeo/ |
	sed -n 's#^app-editors/zeo/\(zeo-[0-9].*\)\.ebuild$#\1#p' | sort -V | tail -n 1)
[[ -n $zeo_pf ]] || die 2 "no app-editors/zeo ebuild at ::bentoo $short"
git -C "$overlay" ls-tree --name-only HEAD dev-lang/rust-bin/ | grep -qxF "dev-lang/rust-bin/rust-bin-$rust.ebuild" ||
	die 2 "::bentoo $short has no dev-lang/rust-bin-$rust"
tag="localhost/shidashi-zeo-builder:$(date -u +%Y%m%d)-$short"

# containerfile — the build, every package from the mounted trees (no context is sent)
containerfile() {
	cat <<EOF
# Generated by shidashi scripts/zeo-builder-image.sh; see its header.
FROM __BASE__
ENV FEATURES="-ipc-sandbox -network-sandbox -pid-sandbox -userfetch" LLVM_SLOT="$llvm_slot"
RUN set -e; \\
    emerge --quiet-build --usepkg --buildpkg --with-bdeps=y --jobs=4 --load-average=16 \\
        =dev-lang/rust-bin-$rust; \\
    eselect rust set rust-bin-$rust; \\
    emerge --quiet-build --usepkg --buildpkg --with-bdeps=y --jobs=4 --load-average=16 \\
        --onlydeps =app-editors/$zeo_pf; \\
    echo 'x11-base/xorg-server xvfb minimal' > /etc/portage/package.use/zz-zeo-builder; \\
    INPUT_DEVICES="" VIDEO_CARDS="" \\
    emerge --quiet-build --usepkg --buildpkg --with-bdeps=y --jobs=4 --load-average=16 \\
        dev-util/cargo-nextest dev-util/rustup x11-base/xorg-server x11-wm/twm \\
        sys-apps/dbus media-fonts/dejavu net-misc/rsync; \\
    rm -rf /var/tmp/portage/*
# rustup's homes are set only here and after: during the emerges they would leak into
# dev-util/rustup's own install phase, whose \`rustup completions\` then tries to create
# them inside Portage's sandbox and is denied
RUN set -e; export RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo; \\
    rustup-init -y --no-modify-path --profile minimal --default-toolchain none; \\
    /opt/cargo/bin/rustup toolchain install $rust --profile minimal --target wasm32-wasip2; \\
    ln -sf /opt/cargo/bin/rustup /usr/local/bin/rustup
# only \`rustup\` is on PATH: rustc and cargo stay the system's (rust-bin), as on the desktop;
# Zeo's test.sh puts rustup's toolchain first itself when it runs the tests
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
LABEL org.bentoo.builder.zeo="$zeo_pf" \\
      org.bentoo.builder.rust="$rust" \\
      org.bentoo.builder.overlay-commit="$commit" \\
      org.bentoo.builder.gentoo-timestamp="$stamp"
CMD ["/bin/bash"]
EOF
}

printf 'worker   %s\nimage    %s\nzeo      %s\nrust     %s (rust-bin from ::bentoo), LLVM_SLOT=%s\n' \
	"$WORKER" "$tag" "$zeo_pf" "$rust" "$llvm_slot"
printf 'gentoo   %s (%s)\noverlay  %s @ %s\nuse      %s\n' "$gentoo" "$stamp" "$overlay" "$short" "$use_file"

if ((DRY_RUN)); then
	printf -- '--- Containerfile\n'
	containerfile
	exit 0
fi

ssh_w() { ssh -o BatchMode=yes "$WORKER" "$@"; }

printf 'event=send-trees\n'
ssh_w "mkdir -p '$BUILDER/trees/gentoo' '$BUILDER/trees/bentoo-$short' '$BUILDER/binpkgs' '$BUILDER/etc' '$BUILDER/tmp' '$BUILDER/context'"
rsync -a --delete --exclude=.git --exclude=distfiles --exclude=packages \
	-e 'ssh -o BatchMode=yes' "$gentoo/" "$WORKER:$BUILDER/trees/gentoo/"
git -C "$overlay" archive --format=tar HEAD | ssh_w "tar -x -C '$BUILDER/trees/bentoo-$short'"
rsync -a -e 'ssh -o BatchMode=yes' "$use_file" "$WORKER:$BUILDER/etc/zeo.use"
containerfile | ssh_w "cat > '$BUILDER/Containerfile.$short'"

printf 'event=build image=%s\n' "$tag"
# shellcheck disable=SC2016  # the remote script expands on the worker
ssh_w bash -s -- "$BUILDER" "$FORK_POINTS" "$short" "$tag" "${ZEO_BUILDER_BASE:-}" <<'REMOTE'
set -euo pipefail
builder=$1 forks=$2 short=$3 tag=$4 base_file=${5:-}  # ssh drops an empty argument
export TMPDIR="$builder/tmp"   # podman cannot stack overlays on the live root's /var/tmp
[[ -n $base_file ]] || base_file=$(cd "$forks" && ls -t -- *-desktop.tar | head -n 1)
[[ -f $forks/$base_file ]] || { echo "no desktop fork point $base_file in $forks" >&2; exit 2; }
base="localhost/shidashi-desktop-base:$(printf '%s' "$base_file" | sha256sum | cut -c1-12)"
if ! podman image exists "$base"; then
	echo "event=import-base file=$base_file image=$base"
	podman import --change CMD=/bin/bash "$forks/$base_file" "$base" >/dev/null
fi
sed -i "s#^FROM __BASE__#FROM $base#" "$builder/Containerfile.$short"
slot=/mnt/work/slots/slot
reserve=()
[[ -x $slot ]] && reserve=("$slot" run --owner "shidashi-image:zeo-builder" --cpus all --memory 24 --wait -1 --)
"${reserve[@]}" podman build --layers=false -t "$tag" \
	--label "org.bentoo.builder.base=$base_file" \
	-v "$builder/trees/gentoo:/var/db/repos/gentoo:ro" \
	-v "$builder/trees/bentoo-$short:/var/db/repos/bentoo:ro" \
	-v "$builder/etc/zeo.use:/etc/portage/package.use/zeo:ro" \
	-v "$builder/binpkgs:/var/cache/binpkgs" \
	-v /mnt/work/cache/distfiles:/var/cache/distfiles \
	-f "$builder/Containerfile.$short" "$builder/context"
digest=$(podman image inspect "$tag" --format '{{.Digest}}')
rustc_v=$(podman run --rm "$tag" rustc --version)
echo "event=built image=$tag digest=$digest rustc=\"$rustc_v\""
# keep the two newest builder images; CreatedAt sorts as text, Created is relative
podman images --filter "reference=localhost/shidashi-zeo-builder" \
	--format '{{.CreatedAt}}|{{.Repository}}:{{.Tag}}' | sort -r | cut -d'|' -f2 | tail -n +3 |
	while read -r old; do podman rmi "$old" >/dev/null 2>&1 && echo "event=pruned image=$old" || true; done
REMOTE
