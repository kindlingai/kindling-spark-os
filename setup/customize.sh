#!/bin/bash
# Runs inside the new root (mmdebstrap customize hook). Adds what the packages do not: the admin
# user, the frozen kernel cmdline, the persistent binds, and which services start.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# admin is uid/gid 1000 on every box and owns the persistent /home, so the numbers must match. No
# password: ssh keys come from the persistent /home/admin/.ssh.
groupadd -g 1000 admin
useradd -u 1000 -g 1000 -G adm,sudo,docker,users -s /bin/bash -M -d /home/admin admin
passwd -l admin
echo "admin ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-admin-nopasswd
chmod 440 /etc/sudoers.d/90-admin-nopasswd

# The spark agent runs as its own user. spark-agent.service grants it CAP_SYS_PTRACE and nothing
# else.
useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin spark-agent

# Persistent state stays on the box's own root disk, which the initramfs leaves at
# /run/sparkos/host. /var/log too, so the journal survives a reboot instead of filling the RAM
# overlay. A site layer adds its own binds the same way.
for d in home var/log var/lib/docker var/lib/containerd; do
  mkdir -p "/$d"
  echo "/run/sparkos/host/$d /$d none bind,nofail 0 0" >> /etc/fstab
done

# The cmdline DGX OS arrives at on these boards: NVIDIA's packages write it as GRUB fragments from
# platform detection at boot, which an image cannot do, so it is fixed here instead. kho=off comes
# with the 7.0 kernel (nvidia-spark-grub-kho).
echo "init_on_alloc=0 iommu.passthrough=0 earlycon=uart,mmio32,0x16A00000 console=tty0 console=ttyS0,921600 crashkernel=1G-:0M initcall_blacklist=tegra234_cbb_init kho=off pci=pcie_bus_safe" > /etc/kernel/cmdline

systemctl set-default multi-user.target
systemctl enable ssh NetworkManager docker containerd nvidia-persistenced systemd-resolved \
  systemd-timesyncd spark-watchdog spark-dmesg-snapshot.timer sparkos-trial-revert.timer \
  mentatd spark-agent dispramd spark-console
# tty1 belongs to spark-console; the other consoles keep their gettys.
systemctl mask getty@tty1.service autovt@tty1.service

# build-rootfs.sh diverted update-initramfs during package installation. It builds the initramfs
# itself once the site layer is in.
rm /usr/sbin/update-initramfs
dpkg-divert --local --rename --remove /usr/sbin/update-initramfs >/dev/null

echo "C.UTF-8 UTF-8" > /etc/locale.gen
ln -sf /usr/share/zoneinfo/UTC /etc/localtime
rm -rf /var/log/*.log
