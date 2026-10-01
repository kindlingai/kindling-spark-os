#!/bin/bash
# Build the setup image for one stack (default: the one stacks.yaml marks default). Run on an arm64
# host with docker, such as a box itself.
#
#   tools/build-image.sh [STACK]
#
# Tags kindling-spark-os:<stack> and :<version>-<stack>, with + spelled - (Docker tags cannot hold
# +), plus :latest and :<version> when STACK is the default. <version> is the repo's VERSION.
set -euo pipefail
# Docker access is root's on these boxes (DGX OS keeps users out of the docker group).
docker info >/dev/null 2>&1 || [ "$(id -u)" = 0 ] || exec sudo -- "$0" "$@"
cd "$(dirname "$0")/.."
default=$(sed -n 's/^default: *//p' stacks.yaml)
stack=${1:-$default}
grep -q "^  $stack:" stacks.yaml || { echo "no stack $stack in stacks.yaml" >&2; exit 2; }
version=$(cat VERSION)
tags=(-t "kindling-spark-os:${stack//+/-}" -t "kindling-spark-os:$version-${stack//+/-}")
[ "$stack" = "$default" ] && tags+=(-t kindling-spark-os:latest -t "kindling-spark-os:$version")
docker build --build-arg STACK="$stack" "${tags[@]}" .
