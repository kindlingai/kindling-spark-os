#!/bin/bash
# Set up kindling spark-os on this GB10 box: check what it needs, build the setup image, and install
# a trial image beside DGX OS.
#
#   ./setup.sh [--check] [--yes] [--stack STACK] [--flavour nvidia-64k|nvidia] [--site DIR] [--no-trial]
#              [--packages "P ..."] [--sources FILE] [--mounts "DIR ..."] [--hostname-kindling]
#              [--connectx-mtu N] [--ethernet-mtu N]
#
#   --check     only check the prerequisites; change nothing
#   --yes       answer yes to both questions (physical access, and building and installing)
#   --stack     a kernel/driver pair from stacks.yaml (default: the one it marks default)
#   --flavour   nvidia-64k (default) or nvidia (4 KiB pages)
#   --site      a site layer directory on this box (default: /etc/kindling-spark-os/site if present)
#   --no-trial  install without making it the next boot
#   --packages  extra packages for the image, from Ubuntu, NVIDIA or a --sources file
#   --sources   an apt source on DGX OS to install from, a .list or .sources file (repeatable)
#   --mounts    more directories to bind from the DGX OS disk, such as /var/tmp
#   --hostname-kindling  name the box kindling-XXXX under spark-os (XXXX from its LAN MAC)
#   --connectx-mtu  RoCE MTU for the ConnectX ports (default 4096; 0 off): raises any port too small
#   --ethernet-mtu  exact MTU for the onboard Ethernet ports (default: as DGX OS sets it)
#
# Tailscale, if DGX OS has it:
#   ./setup.sh --sources /etc/apt/sources.list.d/tailscale.list --packages tailscale
#
# Run it from a checkout or an unpacked download of the repository, on DGX OS or on a running
# spark-os. It needs sudo; it never reboots. Afterwards: reboot, check the box, then run
# sparkos-promote on it, or sparkos-rollback to go back.
#
# Every run is appended to ~/kindling-spark-os-setup.log (KINDLING_SETUP_LOG overrides the path):
# the checks, the answers, and each change to the box as a "change:" line followed by an "undo:"
# line, so a person or an agent can reverse exactly what a run did.
set -uo pipefail
cd "$(dirname "$0")"

options="$*"
check_only=0 yes=0 stack= flavour=nvidia-64k site= trial=1 packages= sources=() mounts= hostname_kindling=0
connectx_mtu=4096 ethernet_mtu=0
while [ $# -gt 0 ]; do
	case $1 in
		--check) check_only=1; shift ;;
		--yes|-y) yes=1; shift ;;
		--stack) stack=$2; shift 2 ;;
		--flavour) flavour=$2; shift 2 ;;
		--site) site=$2; shift 2 ;;
		--no-trial) trial=0; shift ;;
		--packages) packages="$packages $2"; shift 2 ;;
		--sources) sources+=("${2#/run/sparkos/host}"); shift 2 ;;
		--mounts) mounts="$mounts $2"; shift 2 ;;
		--hostname-kindling) hostname_kindling=1; shift ;;
		--connectx-mtu) connectx_mtu=$2; shift 2 ;;
		--ethernet-mtu) ethernet_mtu=$2; shift 2 ;;
		-h|--help) sed -n '2,/^set -uo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "setup.sh: unknown option $1 (try --help)" >&2; exit 2 ;;
	esac
done

if [ -t 1 ]; then ok=$'\e[32mok\e[0m' bad=$'\e[31mFAIL\e[0m' warn=$'\e[33mwarn\e[0m'; else ok=ok bad=FAIL warn=warn; fi

# Everything from here on also goes to the log, without the colour codes.
log=${KINDLING_SETUP_LOG:-$HOME/kindling-spark-os-setup.log}
exec > >(tee >(sed -u 's/\x1b\[[0-9;]*m//g' >> "$log")) 2>&1
echo
echo "=== $(date -u '+%Y-%m-%d %H:%M:%S UTC') setup.sh ${options:-(no options)} on $(hostname), as $(id -un), log $log"
failed=0
pass() { printf '  [%s]   %s\n' "$ok" "$1"; }
fail() { printf '  [%s] %s\n' "$bad" "$1"; [ -n "${2:-}" ] && printf '         fix: %s\n' "$2"; failed=1; }
note() { printf '  [%s] %s\n' "$warn" "$1"; [ -n "${2:-}" ] && printf '         %s\n' "$2"; }

echo "kindling spark-os $(cat VERSION 2>/dev/null || echo '?') setup"
echo

echo "checking this box:"
if [ ! -f VERSION ] || [ ! -f stacks.yaml ] || [ ! -f Dockerfile ]; then
	fail "run setup.sh from the kindling-spark-os repository" "cd into the checkout or unpacked download first"
	exit 1
fi

# Platform: an arm64 GB10 box booted by UEFI.
[ "$(uname -m)" = aarch64 ] && pass "arm64" || fail "this is $(uname -m), not arm64" "run setup.sh on the GB10 box itself"
if command -v lspci >/dev/null && lspci -n | grep -qi '10de:2e12'; then
	pass "NVIDIA GB10 GPU"
else
	fail "no NVIDIA GB10 GPU (PCI 10de:2e12) found" "kindling spark-os only supports GB10 boxes (DGX Spark, ASUS Ascent GX10)"
fi
[ -d /sys/firmware/efi ] && pass "UEFI boot" || fail "not booted by UEFI" "boot the box in UEFI mode"

# Which OS: DGX OS directly, or a running spark-os with DGX OS's disk at /run/sparkos/host.
if mountpoint -q /run/sparkos/host 2>/dev/null; then
	host=/run/sparkos/host
	pass "running spark-os $(cat /etc/kindling/version 2>/dev/null || echo pre-0.9) ($(cat /etc/spark-os-version)); DGX OS root at $host"
else
	host=/
	if grep -q '^VERSION_ID="24.04"' /etc/os-release 2>/dev/null; then
		pass "Ubuntu 24.04 (DGX OS)"
	else
		fail "not Ubuntu 24.04: $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")" "kindling spark-os installs beside DGX OS 7 (Ubuntu 24.04)"
	fi
fi
H=${host%/}
[ -d "$H/boot/grub" ] && pass "GRUB on the DGX OS root" || fail "no $H/boot/grub" "DGX OS must boot with GRUB"
saved=$(grep -a '^saved_entry=' "$H/boot/grub/grubenv" 2>/dev/null | cut -d= -f2)
index='^[0-9>]+$'
if [[ $saved =~ $index && $saved != 0 ]]; then
	fail "GRUB's saved default is the menu index $saved; spark-os's fallback to DGX OS needs an id" \
		"$([ "$host" = / ] && echo sudo || echo enter-dgx-os) grub-set-default 0"
else
	pass "GRUB default: ${saved:-the first entry}"
fi
keys_ok=1
for k in dgx_debian_prod.gpg cuda_debian_prod.gpg; do [ -f "$H/usr/share/keyrings/$k" ] || keys_ok=0; done
[ "$keys_ok" = 1 ] && pass "NVIDIA apt keyrings" ||
	fail "NVIDIA's apt keyrings are missing from $H/usr/share/keyrings" "this needs DGX OS, which installs dgx_debian_prod.gpg and cuda_debian_prod.gpg"

# Tools and privileges.
if [ "$(id -u)" = 0 ]; then
	sudo=
	pass "running as root"
elif sudo -n true 2>/dev/null || { [ "$check_only" = 0 ] && sudo -v; }; then
	sudo=sudo
	pass "sudo"
else
	sudo=sudo
	fail "sudo is not available without a password" "run setup.sh as a user who can sudo"
fi
for t in curl lspci; do command -v "$t" >/dev/null || fail "$t is not installed" "sudo apt-get install -y $t"; done
docker_ok=0
if ! command -v docker >/dev/null; then
	fail "docker is not installed" "DGX OS ships it; reinstall with: sudo apt-get install -y docker-ce docker-buildx-plugin"
elif ! $sudo docker info >/dev/null 2>&1; then
	fail "docker is installed but its daemon does not answer" "sudo systemctl enable --now docker"
else
	docker_ok=1
	pass "docker $($sudo docker version --format '{{.Server.Version}}' 2>/dev/null)"
fi

# Space: the build needs about 6 GB on the DGX OS root, each image keeps 1.6 GB, and a 64k image
# makes a 16 GiB swap file unless one exists.
free_gb=$(( $(df -B1 --output=avail "$H/" | tail -1) >> 30 ))
if [ "$free_gb" -lt 10 ]; then
	fail "only $free_gb GiB free on the DGX OS root disk" "free at least 10 GiB"
else
	pass "$free_gb GiB free on the DGX OS root disk"
	if [ "${flavour}" = nvidia-64k ] && [ ! -f "$H/swap-64k.img" ] && [ "$free_gb" -lt 48 ]; then
		note "under 48 GiB free: install will skip the 16 GiB /swap-64k.img and the image will run without swap" \
			"model loads that DGX OS absorbs by swapping can then be OOM-killed; free more space first"
	fi
fi

# Network: package archives, NVIDIA's repositories, Docker Hub and GitHub.
for url in http://ports.ubuntu.com/ubuntu-ports/ https://repo.download.nvidia.com/baseos/ubuntu/ \
           https://developer.download.nvidia.com/compute/cuda/repos/ https://registry-1.docker.io/v2/ \
           https://github.com/; do
	code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$url")
	case $code in
		000) fail "cannot reach ${url%/}" "check this box's internet access" ;;
		*) pass "reach ${url%/}" ;;
	esac
done

echo
echo "checking what the image will need at boot:"
# Login: sparkos-users brings DGX OS's users in. Without one holding an ssh key, a trial boot has no
# way in and reverts after 10 minutes.
logins=$(awk -F: '$3 >= 1000 && $3 < 60000 && $7 !~ /(nologin|false)$/ {print $1 ":" $6}' "$H/etc/passwd" 2>/dev/null)
keyed=
for l in $logins; do
	[ -s "$H${l#*:}/.ssh/authorized_keys" ] && keyed="$keyed ${l%%:*}"
done
if [ -n "$keyed" ]; then
	pass "ssh login with a key for:$keyed"
else
	note "no DGX OS user has ~/.ssh/authorized_keys" "add your key first, or the trial boot will have no ssh and revert to DGX OS"
fi
[ -f "$H/etc/spark/node.env" ] && pass "/etc/spark/node.env" ||
	note "no /etc/spark/node.env, so mentatd will not start" "see README, Per-node settings"
[ -f "$H/etc/spark/agent.env" ] && pass "/etc/spark/agent.env" ||
	note "no /etc/spark/agent.env, so the spark agent will not start" "see README, Per-node settings"
if [ "$docker_ok" = 1 ]; then
	for c in mentatd spark-agent; do
		policy=$($sudo docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c" 2>/dev/null) || continue
		[ "$policy" = no ] && continue
		note "the $c container restarts on boot ($policy) and would take the same ports as the image's $c" \
			"sudo docker update --restart=no $c"
	done
fi
for src in "${sources[@]}"; do
	if [ ! -f "$H$src" ]; then
		fail "--sources $src: no such file on the DGX OS disk" "give the path as DGX OS sees it, such as /etc/apt/sources.list.d/tailscale.list"
		continue
	fi
	missing=
	for key in $(grep -oE '(signed-by=|Signed-By: *)/[^] ]+' "$H$src" | sed -E 's/^(signed-by=|Signed-By: *)//'); do
		[ -f "$H$key" ] || missing="$missing $key"
	done
	[ -z "$missing" ] && pass "apt source $src" || fail "--sources $src names keyrings missing from DGX OS:$missing"
done
[ -n "$mounts" ] && pass "extra mounts:$mounts"
[ -n "$packages" ] && pass "extra packages:$packages"
# Tailscale on DGX OS but not asked for: the image would come up without it, and a box reached only
# over the tailnet could not be reached for its trial.
if [ -d "$H/var/lib/tailscale" ] && [[ " $packages " != *" tailscale "* ]]; then
	ts=$(ls "$H"/etc/apt/sources.list.d/tailscale.* 2>/dev/null | head -1)
	note "DGX OS runs Tailscale, but the image will not include it" \
		"add: ${ts:+--sources ${ts#$H} }--packages tailscale   (the image keeps this box's tailnet identity)"
fi
[ "$hostname_kindling" = 1 ] && pass "hostname under spark-os: kindling-XXXX from the LAN MAC"
# MTUs the image will change at boot. A port that comes up with a larger MTU than its peer or
# switch allows drops the larger frames, so these are worth a look before the first boot.
for dev in /sys/class/net/en*; do
	[ -e "$dev/device" ] && [ "$(cat "$dev/operstate")" = up ] || continue
	iface=${dev##*/} mtu=$(cat "$dev/mtu")
	if [ "$(basename "$(readlink "$dev/device/driver")")" = mlx5_core ]; then
		[ "$connectx_mtu" -gt 0 ] && [ "$mtu" -lt $((connectx_mtu + 104)) ] &&
			note "ConnectX $iface is up at MTU $mtu, so RoCE runs below $connectx_mtu; the image raises it to $((connectx_mtu + 104))" \
				"make sure the switch or peer on that link accepts $((connectx_mtu + 104))-byte frames, or pass --connectx-mtu 0"
	elif [ "$ethernet_mtu" -gt 0 ] && [ "$mtu" != "$ethernet_mtu" ]; then
		note "Ethernet $iface is up at MTU $mtu; the image sets it to $ethernet_mtu" "make sure the network it is on allows that"
	fi
done
[ -n "$site" ] || { [ -d "$H/etc/kindling-spark-os/site" ] && site=/host/etc/kindling-spark-os/site; }
[ -n "$site" ] && pass "site layer: ${site#/host}" || pass "no site layer"

echo
if [ "$failed" = 1 ]; then
	echo "Fix the FAIL lines above, then run setup.sh again."
	exit 1
fi
if [ "$check_only" = 1 ]; then
	echo "All prerequisites are in place. Run ./setup.sh to build and install."
	exit 0
fi

# ask QUESTION: yes or no from the terminal, recorded in the log. --yes answers yes.
ask() {
	local answer
	if [ "$yes" = 1 ]; then
		echo "$1 yes (--yes)"
		return 0
	fi
	read -r -p "$1 " answer
	echo "answer: ${answer:-<none>}"
	case $answer in y|Y|yes|Yes) return 0 ;; *) return 1 ;; esac
}
if ! ask "Do you have physical access to this machine? While we have taken precautions to harden the setup process, you may need access to the HDMI output, a USB keyboard and potentially the ability to power cycle if something goes wrong. [y/N]?"; then
	echo "Nothing changed."
	exit 0
fi
echo

version=$(cat VERSION)
default_stack=$(sed -n 's/^default: *//p' stacks.yaml)
stack=${stack:-$default_stack}
tag="kindling-spark-os:$version-${stack//+/-}"
echo "This will build $tag and install a kindling spark-os $version image ($flavour, stack $stack)"
[ "$trial" = 1 ] && echo "as this box's next boot, once. DGX OS stays the default." || echo "beside DGX OS, without changing the next boot."
if ! ask "Continue? [y/N]"; then
	echo "Nothing changed."
	exit 0
fi

set -e
echo
echo "building the setup image (a few minutes the first time)..."
build_log=$(mktemp /tmp/kindling-setup-build.XXXXXX.log)
$sudo tools/build-image.sh "$stack" > "$build_log" 2>&1 || {
	tail -20 "$build_log"; echo "setup image build failed; full log: $build_log"; exit 1; }
tags=$(sed -n 's/.*naming to docker.io\/library\/\(kindling-spark-os:[^ ]*\) .*/\1/p' "$build_log" | sort -u | xargs)
printf 'change: built Docker images %s\n  undo: sudo docker rmi %s\n' "$tags" "$tags"
echo "installing (about 4 minutes)..."
args=(install --flavour "$flavour")
[ "$trial" = 1 ] && args+=(--trial)
[ -n "$site" ] && args+=(--site "$site")
[ -n "$packages" ] && args+=(--packages "$packages")
for src in "${sources[@]}"; do args+=(--sources "$src"); done
[ -n "$mounts" ] && args+=(--mounts "$mounts")
[ "$hostname_kindling" = 1 ] && args+=(--hostname-kindling)
args+=(--connectx-mtu "$connectx_mtu" --ethernet-mtu "$ethernet_mtu")
install_out=$(mktemp)
$sudo docker run --rm --privileged --network host -v "$host:/host" "$tag" "${args[@]}" | tee "$install_out"
installed=$(sed -n 's/^installed spark-os //p' "$install_out")
rm -f "$install_out"

grub=sudo
[ "$host" = / ] || grub=enter-dgx-os
echo
echo "Done: kindling spark-os $version image $installed. Next:"
if [ "$trial" = 1 ]; then
	echo "  1. sudo systemctl reboot        (the box boots the new image once)"
else
	echo "  1. $grub grub-reboot sparkos-$installed && sudo systemctl reboot"
fi
echo "  2. ssh back in and check it; within 10 minutes run sparkos-promote to keep it,"
echo "     or do nothing and it reverts to DGX OS by itself."
echo "  3. sparkos-rollback, at any time, goes back to DGX OS."
echo "Every change above is in $log, each with its undo."
