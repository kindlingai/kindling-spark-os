#!/bin/bash
# Print the stacks.yaml entry for a kernel ABI and NVIDIA driver version, resolved against Ubuntu's
# ports archive alone.
#
#   tools/pin-stack.sh 7.0.0-1019 580.178.04-0ubuntu0.24.04.1 > /tmp/entry.yaml
#
# Needs apt (any arm64 Ubuntu 24.04, or a box's DGX OS through enter-dgx-os). The resolver runs in
# a private apt state directory with only the Ubuntu ports sources. The CUDA repository publishes
# its own builds of the same driver under a higher version, so resolving with the host's sources
# could mix the two.
set -euo pipefail
abi=$1      # e.g. 7.0.0-1019
driver=$2   # e.g. 580.178.04-0ubuntu0.24.04.1
series=${driver%%.*}
upstream=${driver%%-*}

state=$(mktemp -d)
trap 'rm -rf "$state"' EXIT
mkdir -p "$state/lists/partial" "$state/cache/archives/partial" "$state/etc/preferences.d"
for suite in noble noble-updates noble-security; do
  echo "deb [arch=arm64] http://ports.ubuntu.com/ubuntu-ports $suite main restricted universe"
done > "$state/etc/sources.list"
: > "$state/status"
apt_opts=(-o "Dir::Etc::SourceList=$state/etc/sources.list" -o "Dir::Etc::SourceParts=/nonexistent"
          -o "Dir::Etc::Preferences=/nonexistent" -o "Dir::Etc::PreferencesParts=$state/etc/preferences.d"
          -o "Dir::State::Lists=$state/lists" -o "Dir::State::Status=$state/status"
          -o "Dir::Cache=$state/cache" -o "APT::Architecture=arm64" -o "APT::Install-Recommends=false")
apt-get "${apt_opts[@]}" -qq update

# The commit behind the driver's tag in NVIDIA/open-gpu-kernel-modules, through GitHub's API
# because DGX OS has no git. The tag is annotated, so it points at a tag object, then the commit.
ogkm=$(python3 - "$upstream" <<'PY'
import json, sys, urllib.request
api = "https://api.github.com/repos/NVIDIA/open-gpu-kernel-modules/git"
obj = json.load(urllib.request.urlopen(f"{api}/ref/tags/{sys.argv[1]}"))["object"]
while obj["type"] == "tag":
    obj = json.load(urllib.request.urlopen(f"{api}/tags/{obj['sha']}"))["object"]
print(obj["sha"])
PY
)

kernel_pkgs=()
for flavour in nvidia nvidia-64k; do
  k=$abi-$flavour
  kernel_pkgs+=("linux-image-$k" "linux-modules-$k" "linux-modules-nvidia-$series-open-$k"
                "linux-modules-nvidia-fs-$k" "linux-tools-$k")
done
driver_pkgs=("nvidia-kernel-common-$series=$driver" "libnvidia-compute-$series=$driver"
             "libnvidia-cfg1-$series=$driver" "nvidia-utils-$series=$driver"
             "nvidia-compute-utils-$series=$driver" "nvidia-firmware-$series-$upstream")

# With an empty status file apt resolves the whole dependency closure. Keep the kernel and NVIDIA
# packages; the rest of the base system comes from the archive at setup time.
apt-get "${apt_opts[@]}" install --print-uris -qq -y "${kernel_pkgs[@]}" "${driver_pkgs[@]}" |
  sed -n "s/^'\([^']*\)' \([^ ]*\) .*/\1 \2/p" |
  grep -E " (linux-[^ ]*${abi}[^ _]*|linux-tools-common|nvidia-[^ ]*|libnvidia-[^ ]*)_" |
  while read -r url file; do
    name=${file%%_*}
    version=${file#*_}
    version=${version%_*}
    sha=$(apt-cache "${apt_opts[@]}" show "$name=$version" | awk '/^SHA256:/ && !seen {print $2; seen = 1}')
    printf '%s\t%s\t%s\t%s\n' "$name" "$version" "$url" "$sha"
  done | sort -u | awk -F'\t' -v abi="$abi" -v drv="$driver" -v ogkm="$ogkm" '
    BEGIN { split(drv, d, "-"); print abi "+" d[1] ":"; print "  kernel_abi: " abi; print "  driver: " drv
            print "  ogkm_commit: " ogkm
            print "  flavours: [nvidia, nvidia-64k]"; print "  debs:" }
    { printf "    - {name: %s, version: \"%s\", url: \"%s\", sha256: %s}\n", $1, $2, $3, $4 }'
