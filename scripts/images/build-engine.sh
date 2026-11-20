#!/usr/bin/env bash
# Builds the image engine (stable-diffusion.cpp's sd-server, with CUDA) from the commit pinned in
# config/image-engine.lock, and installs it where systemd/images/models.json expects it:
#
#   bash scripts/images/build-engine.sh
#
# The source is fetched with network access (one shallow fetch of the pinned commit and its submodules, each
# checked against the commit recorded in the lock). The compile then runs inside bubblewrap with no network and
# a filesystem that holds only /usr, /etc, /opt/cuda, an empty /tmp, the source (read-only, so the build cannot
# plant a git hook in it that runs on your next fetch) and the build directory. There is no home, no /run (the
# Docker API and your session's sockets are there), no /var (journals, core dumps) and no /mnt. The build
# scripts of the engine and its submodules can neither reach the network nor read your files.
#
# Nothing here needs sudo, and nothing here touches the running engine unit: the new binary replaces the old one
# by rename, so an engine that is running keeps the old file until the launcher's 5-minute idle exit.
# Environment: AIHUB_ENGINE_BUILD (build directory, default ~/work/aihub-engine-build), AIHUB_IMAGES_DIR (the
# directory models.json is relative to, default ~/work/aihub-images), JOBS (default 6).
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../.." && pwd)
lock=$repo/config/image-engine.lock
build_root=${AIHUB_ENGINE_BUILD:-$HOME/work/aihub-engine-build}
images_dir=${AIHUB_IMAGES_DIR:-$HOME/work/aihub-images}
jobs=${JOBS:-6}
cuda=/opt/cuda
src=$build_root/src
build=$build_root/build

# The source directory is only ever written by this script, but a git hook or fsmonitor setting that got in there
# would run as you, so the host-side git calls switch those off.
git() { command git -c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.file.allow=never "$@"; }
die() {
    printf 'build-engine: %s\n' "$*" >&2
    exit 1
}
# lockval KEY: the value of "KEY=value" in the lock file
lockval() { sed -n "s/^$1=//p" "$lock" | head -n 1; }

[ -f "$lock" ] || die "missing $lock"
pin=$(lockval PIN)
ggml_pin=$(lockval SUBMODULE_ggml)
webp_pin=$(lockval SUBMODULE_libwebp)
arch=$(lockval CUDA_ARCH)
for v in pin ggml_pin webp_pin arch; do [ -n "${!v}" ] || die "the lock has no value for $v"; done
for tool in git cmake bwrap ninja "$cuda/bin/nvcc" sha256sum; do
    command -v "$tool" >/dev/null || die "missing tool: $tool"
done
[ "$(id -u)" -ne 0 ] || die "run this as your own user, not root"

# --- 1. source, with network, verified commit by commit -----------------------------------------------------------
mkdir -p "$build_root"
if [ ! -d "$src/.git" ]; then
    git init -q "$src"
    git -C "$src" remote add origin https://github.com/leejet/stable-diffusion.cpp.git
fi
if [ "$(git -C "$src" rev-parse -q --verify HEAD 2>/dev/null || true)" != "$pin" ]; then
    git -C "$src" fetch -q --depth 1 origin "$pin"
    git -C "$src" checkout -q --force FETCH_HEAD
fi
[ "$(git -C "$src" rev-parse HEAD)" = "$pin" ] || die "the source is not at the pinned commit $pin"
# The web front end and libwebm are not built (see the cmake flags below), so they are not fetched.
git -C "$src" submodule update -q --init --depth 1 ggml thirdparty/libwebp
sub() { git -C "$src" ls-tree HEAD "$1" | awk '{print $3}'; }
[ "$(sub ggml)" = "$ggml_pin" ] || die "ggml is at $(sub ggml), the lock says $ggml_pin"
[ "$(sub thirdparty/libwebp)" = "$webp_pin" ] || die "libwebp is at $(sub thirdparty/libwebp), the lock says $webp_pin"
for m in ggml thirdparty/libwebp; do
    [ "$(git -C "$src/$m" rev-parse HEAD)" = "$(sub "$m")" ] || die "submodule $m is not at the commit the pin records"
done

# --- 2. compile, with no network ------------------------------------------------------------------------------------
rm -rf "$build"
mkdir -p "$build"
# What the compile needs, and nothing else. /bin, /lib, /lib64 and /sbin are symlinks into /usr on this system.
# If a build step fails for want of a path, add that path here: it prints the real error.
sandbox() {
    bwrap --die-with-parent --unshare-all --unshare-net \
        --ro-bind /usr /usr --ro-bind /etc /etc --ro-bind "$cuda" "$cuda" \
        --symlink usr/bin /bin --symlink usr/bin /sbin --symlink usr/lib /lib --symlink usr/lib /lib64 \
        --proc /proc --dev /dev --tmpfs /tmp \
        --ro-bind "$src" "$src" --bind "$build" "$build" \
        --setenv HOME "$build" --setenv TMPDIR /tmp --setenv PATH "$cuda/bin:/usr/local/bin:/usr/bin:/bin" \
        "$@"
}
sandbox cmake -S "$src" -B "$build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DSD_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES="$arch" \
    -DCMAKE_CUDA_COMPILER="$cuda/bin/nvcc" \
    -DSD_WEBP=ON -DSD_WEBM=OFF -DSD_SERVER_BUILD_FRONTEND=OFF
sandbox nice -n 10 cmake --build "$build" --target sd-server -j "$jobs"

bin=$(find "$build" -type f -name sd-server -perm -u+x | head -n 1)
[ -n "$bin" ] || die "the build produced no sd-server"

# --- 3. install by rename, record what was built --------------------------------------------------------------------
mkdir -p "$images_dir/bin"
install -m 0755 "$bin" "$images_dir/bin/sd-server.new"
mv -f "$images_dir/bin/sd-server.new" "$images_dir/bin/sd-server"
sum=$(sha256sum "$images_dir/bin/sd-server" | awk '{print $1}')
{
    echo "sd-server built $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "commit $pin (ggml $ggml_pin, libwebp $webp_pin)"
    echo "sha256 $sum"
    echo "nvcc $("$cuda/bin/nvcc" --version | sed -n 's/.*release \([0-9.]*\),.*/\1/p;s/^Build //p' | paste -sd' ')"
    echo "gcc $(gcc -dumpfullversion)"
    echo "cmake $(cmake --version | head -n 1 | awk '{print $3}')"
    echo "runpath $(readelf -d "$images_dir/bin/sd-server" | sed -n 's/.*\(RUNPATH\|RPATH\).*\[\(.*\)\]/\2/p')"
} >"$images_dir/bin/BUILD.txt"

# Record the binary in the lock so that a later change of the file is visible in git.
tmp=$(mktemp)
{ grep -v '^BINARY_SHA256=\|^BUILT_WITH=' "$lock"; echo "BINARY_SHA256=$sum"; echo "BUILT_WITH=$(paste -sd';' <"$images_dir/bin/BUILD.txt" | cut -c1-400)"; } >"$tmp"
mv "$tmp" "$lock"

printf 'installed %s\nsha256    %s\n' "$images_dir/bin/sd-server" "$sum"
