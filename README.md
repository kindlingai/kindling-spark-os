# kindling-spark-os

A minimal, read-only OS image for NVIDIA GB10 boxes (DGX Spark, ASUS Ascent GX10), installed beside
the box's DGX OS and booted from GRUB.

`kindling-spark-os` gets you an extra 4GB to run models: first, by using a 64kB kernel, and second,
by making use of GPU RAM that normally goes unused.

The image is an erofs file on the box's own root disk. At boot the initramfs mounts it under a RAM
overlay, copies the box's identity in (hostname, machine-id, ssh host keys, network config,
`/etc/spark`), and binds `/home`, `/var/log`, `/var/lib/docker` and `/var/lib/containerd` from
the root disk. Every boot starts from the same image. DGX OS stays installed as the rescue system
and owns GRUB.

The image carries:

- the kernel and NVIDIA driver of one supported pair (`stacks.yaml`), Canonical-signed, so Secure
  Boot keeps working
- Docker and the NVIDIA container toolkit, RDMA and ConnectX tools, NVIDIA's DGX tuning packages
- `mentatd`, the [mentat](https://github.com/mmastrac/mentat) node daemon
- the [spark agent](https://github.com/mmastrac/spark-agent), which registers its MCP endpoint
  with mentat
- `dispramd`, which lends the GPU's 2 GiB display carveout to CUDA (see [dispram](#dispram))
- `spark-watchdog`, a host log snapshot for the agent, and a cluster status console on tty1

## Supported stacks

Canonical builds each signed NVIDIA module package for one kernel ABI against exactly one driver
version, so a stack is that pair. `stacks.yaml` lists the pairs that have passed a trial boot, with
every `.deb`'s URL and sha256, and names a default. `tools/pin-stack.sh ABI DRIVER` writes a new
entry.

Ubuntu does not snapshot its ports archive, so an old pair's `.debs` can disappear from the mirror.
The setup image for a pair contains them, which makes each image tag an archive of its pair.

## Build the setup image

On an arm64 host with Docker (a box will do):

    tools/build-image.sh            # the default stack, tagged :<stack> (+ as -) and :latest
    tools/build-image.sh 7.0.0-1019+580.178.04

## Install on a box

From DGX OS:

    docker run --rm --privileged --network host -v /:/host kindling-spark-os install --trial

From a running spark-os, mount the DGX OS root disk instead:

    docker run --rm --privileged --network host -v /run/sparkos/host:/host kindling-spark-os install --trial

`install` builds the image (about 4 minutes), copies it to `/images/spark-os-VERSION.erofs` with its
kernel and initramfs in `/boot/sparkos/VERSION/`, and adds a GRUB entry. `--trial` makes that entry
the next boot, once. Reboot when ready.

Options: `--flavour nvidia-64k` (default; 64 KiB pages return about 2 GiB on a 128 GiB box, with
THP off so `vm.min_free_kbytes` does not grow to 5% of RAM) or `nvidia`; `--version NAME`; `--site DIR` (below). `list` shows the installed images and GRUB's
state, `stack` the pair this setup image carries.

### Swap

spark-os uses the DGX OS disk's swap: DGX OS makes a 16 GiB `/swap.img`, and without swap a model
load whose peak DGX OS absorbs gets OOM-killed. A swap header records its page size, so a 64k image
cannot use `/swap.img`. For a 64k image, `install` makes a parallel `/swap-64k.img` of the same size
on that disk, once. It skips this with a warning if less than 32 GiB would stay free
(`MIN_FREE_GIB`). At boot, `sparkos-swap` turns on whichever file matches the running page size.

### Trial and promote

A new image boots in trial mode: `sparkos.trial panic=10` on the cmdline. A failed mount panics and
reboots, and an unconfirmed boot reboots after 10 minutes. GRUB's one-shot entry is gone by then,
so both land back in DGX OS. If you don't get SSH up and running, it reverts back.

Once the image is good, run `sparkos-promote` on it. That confirms the boot, drops the trial flag,
and makes the image GRUB's default. DGX OS stays in the menu.

`enter-dgx-os [COMMAND]` runs a shell or command in the DGX OS install from spark-os, in a private
mount namespace. Use it for anything the image leaves out: apt, update-grub, grub-reboot.

## Per-node settings

These live in `/etc/spark` on each box's own disk, and the initramfs copies them in:

- `node.env` for `mentatd`: `MENTAT_NODE_IP`, `MENTAT_PEERS`, `MENTAT_ANNOUNCE_IFACES`,
  `MENTAT_SECRET_FILE`. Without it `mentatd` does not start.
- `agent.env` for the spark agent: `MENTAT_ROUTER_URL` and `ALLOWED_SOURCES`. Without it the agent
  does not start.

Boxes moving over from the container deployments should stop the `mentatd` and `spark-agent`
containers (`docker update --restart=no` and `docker stop`). Otherwise they take the same ports.

## Site layer

Anything specific to one fleet goes in a site layer, so this repo carries no fleet addresses.
`install --site DIR`, or `/etc/kindling-spark-os/site` on the DGX OS root when it exists:

- `DIR/root/` is copied over the image (units, scripts, config)
- `DIR/packages.txt` lists extra packages
- `DIR/customize.d/*.sh` run inside the image after the base customize step, in order. Use them to
  enable the site's units and add `/etc/fstab` lines, such as more binds from `/run/sparkos/host`.

## Services

| Unit | What |
|---|---|
| `mentatd` | mentat node daemon (needs `/etc/spark/node.env`) |
| `spark-agent` | status page and MCP tools on :8090, runs as `spark-agent` with `CAP_SYS_PTRACE` only (needs `/etc/spark/agent.env`) |
| `dispramd` | display carveout lender, on the validated driver only |
| `spark-watchdog` | pets the SBSA watchdog while the box can still fork |
| `spark-dmesg-snapshot.timer` | writes dmesg, docker and systemd state to `/var/log/spark` for the agent each minute |
| `spark-console` | cluster status on tty1; Esc to log in |
| `sparkos-trial-revert.timer` | reverts an unconfirmed trial boot |

## dispram

`dispramd` lends the 2 GiB display carveout, which the driver never uses on GB10, to CUDA processes
as ordinary device memory. A patch for vLLM, and a plugin for stock images, put the tail of the KV
cache there. See [dispram/README.md](dispram/README.md). It is its own AGPL-3.0 directory.

## Licensing

This repository is AGPL-3.0. The images it builds are collections of separately licensed packages,
and each keeps its own license:

- the Linux kernel: GPL-2.0. Source: Ubuntu's `linux-nvidia-7.0` and `linux-signed-nvidia-7.0`
  source packages for the stack's version.
- NVIDIA's open kernel modules: dual MIT/GPL-2.0. Source: `linux-restricted-modules-nvidia-7.0`,
  and [open-gpu-kernel-modules](https://github.com/NVIDIA/open-gpu-kernel-modules) at the driver's tag.
- NVIDIA's userspace driver and GSP firmware: NVIDIA's driver license. Section 1.1(d) of that
  license allows distributing them unmodified, with the license, for use with an open-source kernel.
  The license text is in each package's `copyright` file.
- NVIDIA's DGX tuning packages (`nvidia-spark-limits`, `nv-cpu-governor`, `nvidia-kernel-defaults`
  and others): proprietary, with no right to redistribute. The setup image does not carry them.
  `install` fetches them from NVIDIA's repository with the box's own apt keyring. A built image
  contains them, so do not publish one.
- `dispram/rmlist.c` compiles against NVIDIA's open-gpu-kernel-modules headers (MIT).
- Everything else comes from Ubuntu under its usual licenses.
