#!/bin/sh
# Rebuild the exact upstream source with the small auditable render-lead patch.
# Usage: build-airplay.sh /absolute/build/directory /absolute/output/binary
set -eu
source_dir="${1:?build directory required}"
output_file="${2:?output executable required}"
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
test ! -e "$source_dir"
git clone --depth 1 --branch v0.5.9 \
    https://github.com/music-assistant/airplay-cli.git "$source_dir"
test "$(git -C "$source_dir" rev-parse HEAD)" = 0a58c239d5fb3cf8927b9707a71c6fe897266b6a
# The Makefile consumes these immediate repositories' pinned headers/static
# archives. Do not recursively clone unrelated codec sources/Windows projects.
git -C "$source_dir" submodule update --init --depth 1 libraop
git -C "$source_dir/libraop" submodule update --init --depth 1 \
    crosstools dmap-parser libcodecs libmdns libopenssl
patch --batch --fuzz=0 -d "$source_dir" -p1 < "$script_dir/render-lead.patch"
case "$(uname -m)" in
    x86_64) platform=x86_64 ;;
    aarch64|arm64) platform=aarch64 ;;
    *) echo 'Unsupported AirPlay build architecture' >&2; exit 1 ;;
esac
make -C "$source_dir" -j2 STATIC=1 CC=gcc CXX=g++ HOST=linux PLATFORM="$platform" VERSION=0.5.9-live1
install -m 755 "$source_dir/bin/cliairplay-linux-$platform" "$output_file"
"$output_file" --render-lead 2000 --check
"$output_file" --render-lead 250 --check
if "$output_file" --render-lead 0 --check; then
    echo 'Native render-lead validation failed' >&2
    exit 1
fi
