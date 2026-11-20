#!/usr/bin/env bash
# Puts the image model files named in config/image-models.lock under the models directory and checks every one
# with a plain sha256sum:
#
#   bash scripts/images/fetch-models.sh
#
# A file that is already there with the right size is kept. Otherwise it is hardlinked from the lab copy
# (AIHUB_LAB_MODELS) or from another file of the same sha256 in the models directory, and only then downloaded
# from Hugging Face at the pinned revision. A download goes to a temporary name, so a failed or partial one never
# stands in for the real file; `curl -C -` resumes it on the next run. Nothing needs a token or sudo.
# Environment: AIHUB_IMAGES_DIR (the directory models.json is relative to, default ~/work/aihub-images),
# AIHUB_LAB_MODELS (default ~/work/aihub-image-lab/models).
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
lock=$here/../../config/image-models.lock
images_dir=${AIHUB_IMAGES_DIR:-$HOME/work/aihub-images}
lab=${AIHUB_LAB_MODELS:-$HOME/work/aihub-image-lab/models}
models=$images_dir/models

die() {
    printf 'fetch-models: %s\n' "$*" >&2
    exit 1
}
[ -f "$lock" ] || die "missing $lock"
mkdir -p "$models"

sums=$(mktemp)
trap 'rm -f "$sums"' EXIT

while IFS=$'\t' read -r dest bytes sha repo rev path _licence; do
    case $dest in '' | '#'*) continue ;; esac
    case $dest in /* | *..*) die "unsafe destination in the lock: $dest" ;; esac
    target=$models/$dest
    mkdir -p "$(dirname "$target")"
    printf '%s  %s\n' "$sha" "$target" >>"$sums"

    if [ -f "$target" ] && [ "$(stat -c %s "$target")" = "$bytes" ]; then
        continue
    fi
    # A hardlink from a file that already has this content (the lab copy, or the same text encoder under the
    # other model), after checking the content; a file that does not match is not used.
    for candidate in "$lab/$dest" "$models"/*/"$(basename "$dest")"; do
        [ -f "$candidate" ] && [ "$candidate" != "$target" ] && [ "$(stat -c %s "$candidate")" = "$bytes" ] || continue
        if [ "$(sha256sum "$candidate" | awk '{print $1}')" = "$sha" ]; then
            ln -f "$candidate" "$target"
            printf 'linked   %s\n' "$dest"
            continue 2
        fi
    done
    printf 'download %s (%s bytes)\n' "$dest" "$bytes"
    curl --fail --location --silent --show-error -C - --proto '=https' --tlsv1.2 \
        -o "$target.part" "https://huggingface.co/$repo/resolve/$rev/$path"
    mv -f "$target.part" "$target"
done <"$lock"

# The check itself: every file in the lock must hash to the recorded value.
sha256sum -c "$sums"
