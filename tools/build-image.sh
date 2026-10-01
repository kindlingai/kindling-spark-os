#!/bin/bash
# Build the setup image for one stack (default: the one stacks.yaml marks default). Run on an arm64
# host with docker, such as a box itself.
#
#   tools/build-image.sh [STACK]
#
# Tags kindling-spark-os:<stack> with + spelled - (Docker tags cannot hold +), and
# kindling-spark-os:latest when STACK is the default.
set -euo pipefail
cd "$(dirname "$0")/.."
default=$(sed -n 's/^default: *//p' stacks.yaml)
stack=${1:-$default}
grep -q "^  $stack:" stacks.yaml || { echo "no stack $stack in stacks.yaml" >&2; exit 2; }
tags=(-t "kindling-spark-os:${stack//+/-}")
[ "$stack" = "$default" ] && tags+=(-t kindling-spark-os:latest)
docker build --build-arg STACK="$stack" "${tags[@]}" .
