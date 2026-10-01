#!/bin/bash
# Build the read-only root image. Called by kindling-setup with:
#   OUT      output directory: spark-os.erofs, vmlinuz, initrd.img and cmdline land here
#   VERSION  the image's name
#   FLAVOUR  nvidia or nvidia-64k
#   SITE     optional site layer directory (see kindling-setup)
#   HOST     the DGX OS root, for NVIDIA's apt keyrings
#
# Package sources, highest priority first:
#   the stack repo  this pair's kernel and driver .debs, served on 127.0.0.1 and pinned above
#                   everything else: the CUDA repository publishes its own builds of the same
#                   driver under higher version numbers
#   Ubuntu ports    the base system
#   NVIDIA          DGX tuning packages (baseos) and the container toolkit (CUDA), fetched with the
#                   box's own keyrings
#
# update-initramfs is diverted to /bin/true while packages install: postinsts that call it without
# a kernel version fall back to the running kernel, which need not be the image's. customize.sh
# restores it and builds the initramfs once. nvidia-spark-limits' postinst calls update-notifier's
# notify-reboot-required without depending on it, so a no-op stands in for that.
set -euo pipefail
K=/opt/kindling
# shellcheck source=/dev/null
. "$K/stack/stack.env"
kver=$KERNEL_ABI-$FLAVOUR
work=$OUT/work
rm -rf "$work" && mkdir -p "$work"

port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
python3 -m http.server --bind 127.0.0.1 --directory "$K/stack" "$port" >/dev/null 2>&1 &
server=$!
trap 'kill $server 2>/dev/null' EXIT

subst() {
  sed -e 's/#.*//' -e "s/@KVER@/$kver/g" -e "s/@SERIES@/$DRIVER_SERIES/g" \
      -e "s/@DRIVER@/$DRIVER/g" -e "s/@DRIVER_UPSTREAM@/$DRIVER_UPSTREAM/g" "$@"
}
pkgs=$(subst "$K/setup/packages.txt" ${SITE:+$( [ -f "$SITE/packages.txt" ] && echo "$SITE/packages.txt")} |
       xargs | tr ' ' ',')

# Everything that is not a package, as one root-owned tar: the overlay, mentatd, the spark agent and
# dispram.
stage=$work/stage
mkdir -p "$stage/usr/local/bin" "$stage/opt" "$stage/etc/kindling"
cp -a "$K/overlay/." "$stage/"
install -m755 "$K/mentatd/mentatd" "$K/mentatd/mentatd-probe-machine" "$stage/usr/local/bin/"
install -m755 "$K/agent/spark-memory.py" "$stage/usr/local/bin/"
install -d "$stage/opt/spark-agent" && cp -a "$K/agent/lib" "$stage/opt/spark-agent/lib"
install -m755 "$K/agent/spark-agent.py" "$stage/opt/spark-agent/"
install -d "$stage/opt/kindling/dispram"
cp -a "$K/dispram/." "$stage/opt/kindling/dispram/"
cp -a "$K/stack/stack.env" "$stage/etc/kindling/stack.env"
if [ "${DISPRAM_VALIDATED:-0}" = 1 ]; then
  echo "DISPRAM_DRIVER=$DRIVER_UPSTREAM" > "$stage/etc/kindling/dispram.env"
fi
tar --owner=0 --group=0 -C "$stage" -cf "$work/overlay.tar" .
site_hooks=()
if [ -n "${SITE:-}" ]; then
  if [ -d "$SITE/root" ]; then
    tar --owner=0 --group=0 -C "$SITE/root" -cf "$work/site.tar" .
    site_hooks+=(--customize-hook="tar-in $work/site.tar /")
  fi
  if [ -d "$SITE/customize.d" ]; then
    site_hooks+=(--customize-hook='mkdir -p "$1/tmp/site.d"'
                 --customize-hook="sync-in $SITE/customize.d /tmp/site.d"
                 --customize-hook='for f in "$1"/tmp/site.d/*.sh; do [ -f "$f" ] && chroot "$1" /bin/bash "/tmp/site.d/${f##*/}"; done; rm -rf "$1/tmp/site.d"')
  fi
fi

# mmdebstrap runs apt from this container, so signed-by paths resolve here, not in the new root.
install -m644 "$HOST/usr/share/keyrings/dgx_debian_prod.gpg" "$HOST/usr/share/keyrings/cuda_debian_prod.gpg" \
  /usr/share/keyrings/

ubuntu=http://ports.ubuntu.com/ubuntu-ports
comps="main restricted universe"
nvidia_baseos=https://repo.download.nvidia.com/baseos/ubuntu/noble/arm64/
cuda=https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa/

mmdebstrap --mode=root --variant=minbase --architectures=arm64 --include="$pkgs" \
  --aptopt='APT::Install-Recommends "false"' \
  --setup-hook='mkdir -p "$1/usr/share/keyrings" "$1/etc/apt/preferences.d"' \
  --setup-hook="cp $HOST/usr/share/keyrings/dgx_debian_prod.gpg $HOST/usr/share/keyrings/cuda_debian_prod.gpg \"\$1/usr/share/keyrings/\"" \
  --setup-hook='printf "Package: *\nPin: origin \"127.0.0.1\"\nPin-Priority: 1001\n" > "$1/etc/apt/preferences.d/kindling-stack"' \
  --essential-hook='chroot "$1" dpkg-divert --local --rename --add /usr/sbin/update-initramfs >/dev/null && ln -s /bin/true "$1/usr/sbin/update-initramfs"' \
  --essential-hook='mkdir -p "$1/usr/share/update-notifier" && printf "#!/bin/sh\nexit 0\n" > "$1/usr/share/update-notifier/notify-reboot-required" && chmod 755 "$1/usr/share/update-notifier/notify-reboot-required"' \
  --customize-hook="tar-in $work/overlay.tar /" \
  --customize-hook="copy-in $K/setup/customize.sh /tmp" \
  --customize-hook='chroot "$1" /bin/bash /tmp/customize.sh && rm -f "$1/tmp/customize.sh"' \
  "${site_hooks[@]}" \
  --customize-hook='chroot "$1" update-initramfs -c -k all' \
  --customize-hook='rm -rf "$1"/etc/apt/sources.list* "$1"/etc/apt/preferences.d/kindling-stack "$1"/var/lib/apt/lists/* "$1"/var/cache/apt/*' \
  noble "$work/rootfs" \
  "deb [trusted=yes] http://127.0.0.1:$port/ ./" \
  "deb $ubuntu noble $comps" \
  "deb $ubuntu noble-updates $comps" \
  "deb $ubuntu noble-security $comps" \
  "deb [signed-by=/usr/share/keyrings/dgx_debian_prod.gpg] $nvidia_baseos noble common dgx" \
  "deb [signed-by=/usr/share/keyrings/dgx_debian_prod.gpg] $nvidia_baseos noble-updates common dgx" \
  "deb [signed-by=/usr/share/keyrings/cuda_debian_prod.gpg] $cuda /"

echo "$VERSION" > "$work/rootfs/etc/spark-os-version"
# On the 64k kernel a PMD huge page is 512 MiB. When THP is enabled, khugepaged raises
# vm.min_free_kbytes to hold a few free pageblocks of that size per zone, which the kernel caps at
# 5% of RAM: 6.2 GiB on a 128 GiB box, where the 4k kernel keeps 44 MB. That costs more than the
# 64k kernel's 2.1 GiB of smaller page tables gives back. 512 MiB THPs almost never form here, and
# GPU memory does not use them, so THP is off and the reserve stays at the kernel's own ~45 MB.
if [ "$FLAVOUR" = nvidia-64k ]; then
  sed -i 's/$/ transparent_hugepage=never/' "$work/rootfs/etc/kernel/cmdline"
fi
cp "$work/rootfs/boot/vmlinuz-$kver" "$OUT/vmlinuz"
cp "$work/rootfs/boot/initrd.img-$kver" "$OUT/initrd.img"
cp "$work/rootfs/etc/kernel/cmdline" "$OUT/cmdline"
# 4 KiB blocks, mkfs's default, pinned because both flavours boot this format. On a 64k host mkfs
# warns that compressed blocks smaller than a page are unsupported; the 64k kernel mounts them anyway.
mkfs.erofs -b 4096 -zlz4hc "$OUT/spark-os.erofs" "$work/rootfs" >/dev/null
rm -rf "$work"
ls -l "$OUT/spark-os.erofs"
