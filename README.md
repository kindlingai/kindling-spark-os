# kindling-spark-os

*Kindling Spark OS is in beta. Visit the discord for support https://discord.gg/M7XTrRJW3*

A minimal, read-only OS image for NVIDIA GB10 boxes (DGX Spark, ASUS Ascent GX10), installed beside
the box's DGX OS and booted from GRUB.

`kindling-spark-os` gives models about 4 GB more memory than DGX OS: about 2 GiB from a kernel with
64 KiB pages, and the 2 GiB display carveout, which the GPU driver leaves unused (see
[dispram](#dispram)).

![The kindling spark-os status screen on tty1: image version, addresses, GPU, memory, containers and the mentat cluster](docs/status-screen.png)

The image is an erofs file on the DGX OS disk, and every boot starts from the same image. At boot:

- the initramfs mounts the image read-only under a RAM overlay
- the initramfs copies in the box's identity: hostname, machine-id, ssh host keys, network
  config and `/etc/spark`
- systemd binds `/home`, `/srv`, `/var/log`, `/var/lib/docker`, `/var/lib/containerd`,
  `/var/lib/spark-watchdog` and `/var/lib/nfs` from the DGX OS disk. `--mounts` adds more.
- `sparkos-users` copies in DGX OS's login users (uid 1000 and up) and `/etc/sudoers.d`

Users log in with their DGX OS passwords and keys, and keep their DGX OS group memberships. If a
user's login shell is missing from the image, that user gets `/bin/bash`. The image has a fallback
`admin` user at uid 1000, and `sparkos-users` removes it when DGX OS has a user with uid 1000.
`/etc/subuid` and `/etc/subgid` stay on DGX OS.

DGX OS stays installed as the rescue system and manages GRUB.

The image carries:

- the kernel and NVIDIA driver of one supported pair (`stacks.yaml`), signed by Canonical for
  Secure Boot
- Docker and the NVIDIA container toolkit, RDMA and ConnectX tools, NVIDIA's DGX tuning packages
- `mentatd`, the [mentat](https://github.com/mmastrac/mentat) node daemon
- the [spark agent](agent/): a status page and MCP tools, registered with mentat
- `dispramd`, which lends the GPU's 2 GiB display carveout to CUDA (see [dispram](#dispram))
- `spark-watchdog`, a host log snapshot for the agent, and a cluster status console on tty1

## Quick start

On the box, from DGX OS, in a checkout or unpacked download of this repository:

    ./setup.sh --check              # what is missing, changing nothing
    ./setup.sh --secret CLUSTER_KEY  # build the setup image and install a trial image
    sudo systemctl reboot

`CLUSTER_KEY` is mentat's key, and every box in a cluster needs the same one. Make one for a new
cluster with `openssl rand -hex 32`, and pass it to `setup.sh` on each box. Leave out `--secret` on
a box whose `/etc/spark/node.env` already names a key.

`setup.sh` checks the box first: an arm64 GB10, UEFI boot, DGX OS (or a running spark-os), GRUB,
NVIDIA's apt keyrings, sudo, Docker, disk space, and the package and image hosts. Next, `setup.sh`
warns about the new image's needs at boot: a DGX OS user with an ssh key, the `/etc/spark`
settings, and containers that would take the same ports.

Before changing anything, `setup.sh` asks whether you have physical access to the box. A failed
boot can need its HDMI output, a USB keyboard or a power cycle. `setup.sh` then builds the setup
image and runs `kindling-setup install --trial`. The reboot is up to you.

`setup.sh` appends every run to `~/kindling-spark-os-setup.log`, each line stamped with its UTC
time: the checks, your answers, and each change to the box as a `change:` line followed by its
`undo:` line. A person or an agent can reverse a run from the log. Before overwriting a file, setup copies it to
`/var/lib/kindling-spark-os/backup/<time>/`.

After the reboot, check the box, then run `sparkos-promote` within 10 minutes to keep the image.
Otherwise the box goes back to DGX OS by itself. `sparkos-rollback` goes back at any time.

## Supported stacks

Canonical builds each signed NVIDIA module package for one kernel ABI against exactly one driver
version, so a stack is that pair. `stacks.yaml` lists the pairs that have passed a trial boot, with
every `.deb`'s URL and sha256, and names a default. `tools/pin-stack.sh ABI DRIVER` writes a new
entry.

Ubuntu does not snapshot its ports archive, so an old pair's `.debs` can disappear from the mirror.
The setup image for a pair keeps a copy of them.

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

Options:

- `--flavour nvidia-64k` (default) or `nvidia`. 64 KiB pages return about 2 GiB on a 128 GiB box.
  The 64k flavour boots with THP off, which keeps `vm.min_free_kbytes` near 45 MB. With THP on, it
  grows to 5% of RAM. Models on the 64k flavour must load their weights eagerly: see [vLLM](#vllm).
- `--version NAME`. The default is the release, kernel ABI and flavour, such as `0.9-1019-64k`, the
  same on every box, with `-2`, `-3`, ... when that name is already installed on the box.
- `--packages "P ..."`: extra packages, from Ubuntu, NVIDIA's repositories or a `--sources` file.
- `--sources FILE` (repeatable): an apt source on DGX OS, a `.list` or `.sources` file. The
  file's keyrings come from DGX OS too.
- `--mounts "DIR ..."`: more directories to bind from the DGX OS disk. Recipes that keep weights or
  compose directories in `/var/tmp` want `--mounts /var/tmp`. System directories are refused.
- `--hostname-kindling`: under spark-os, name the box `kindling-XXXX`, where XXXX is the last four
  hex digits of its LAN port's MAC (NVIDIA names boxes `gx10-XXXX` the same way). DGX OS keeps its
  own hostname. spark-os reads the name from `/etc/kindling-spark-os/hostname` on the DGX OS disk.
  Delete that file to go back.
- `--connectx-mtu N`: the RoCE MTU for the ConnectX ports, default 4096 (`0` turns it off). RoCE
  runs at the largest IB MTU that fits in the Ethernet MTU less 88 bytes, so a port at the usual
  1500 runs RoCE at 1024. At boot, and whenever a link comes up, a ConnectX port too small for N is
  raised to N + 104 (4200 for 4096). A larger MTU, such as 9000, stays. The switch or peer on that
  link must accept the larger frames. `setup.sh --check` lists the ports the image will raise.
- `--ethernet-mtu N`: an exact MTU for the other Ethernet ports, such as the RJ45 port. By default
  they keep DGX OS's setting.
- `--site DIR` (below).

`setup.sh` takes the same options. `list` shows the installed images and GRUB's state. `stack`
shows this setup image's kernel and driver pair.

### Swap

DGX OS swaps to a 16 GiB `/swap.img`. Some model loads peak above RAM and need it, and without swap
they are OOM-killed. A swap header records its page size, so a 64k image cannot use `/swap.img`.
For a 64k image, `install` makes a second file of the same size, `/swap-64k.img`, once. `install`
skips that file, with a warning, when less than 32 GiB would stay free (`MIN_FREE_GIB`). At boot,
`sparkos-swap` turns on the file that matches the running page size.

### Trial and promote

A new image boots in trial mode: `sparkos.trial panic=10` on the cmdline. A panic reboots. An
unconfirmed boot reboots 10 minutes after it starts. GRUB's one-shot entry is spent by then, so both
land back in DGX OS. To keep a trial running past 10 minutes without promoting it, run
`sudo touch /run/sparkos-confirmed`. That lasts for this boot only.

Once the image is good, run `sparkos-promote` on it. That confirms the boot, drops the trial flag,
and makes the image GRUB's default. `sparkos-promote` refuses if `update-grub` fails or the new entry
still carries the trial flag. DGX OS stays in the menu.

A promoted image still falls back by itself. Each spark-os entry calls GRUB's `recordfail` and
boots with `panic=30`, and `sparkos-boot-ok.service` clears the flag once the image reaches
multi-user. If a boot fails before then (a panic, a missing file, or a hang after spark-watchdog
starts), GRUB's next boot goes to DGX OS (entry 0) without anyone at the console. A hard hang
earlier in boot needs a power cycle, and the same fallback then applies.

### Rollback

`sparkos-rollback` makes DGX OS the default boot again and reboots. `sparkos-rollback VERSION` goes
back to an earlier installed image instead: as the default if that image was promoted, or as a
one-shot trial if it was not. `--no-reboot` sets GRUB without rebooting. Both forms cancel any
queued one-shot boot and clear GRUB's recordfail flag. `install` puts the same command in
the DGX OS root, so it works from either OS.

`enter-dgx-os [COMMAND]` runs a shell or command in the DGX OS install from spark-os, in a private
mount namespace. Use `enter-dgx-os` for tools the image does not carry: apt, update-grub, grub-reboot.

## Per-node settings

These files are in `/etc/spark` on each box's own disk. The initramfs copies them in at boot.

- `node.env` for `mentatd`: `MENTAT_SECRET_FILE`, and optionally `MENTAT_PEERS`,
  `MENTAT_ANNOUNCE_IFACES` and `MENTAT_NODE_IP`. Without it `mentatd` does not start.
  `setup.sh --secret` writes one with the key alone, which is enough: `mentatd` waits for the LAN,
  uses its address, and finds the other boxes by their broadcasts on the same network.
- `agent.env` for the spark agent, optional. The agent reads the cluster from the local `mentatd`
  and answers loopback and the box's own subnets. `MENTAT_ROUTER_URL` names the router: the page
  links to it, and the agent always lets it in. `ALLOWED_SOURCES` replaces the subnets with
  comma-separated CIDR blocks or address prefixes.

Boxes moving over from the container deployments should stop the `mentatd` and `spark-agent`
containers (`docker update --restart=no` and `docker stop`). Otherwise the containers take the same ports.

## Third-party software

### Tailscale

On a box that runs Tailscale under DGX OS, install it in the image from the same apt source, and
bind its state from the DGX OS disk:

    ./setup.sh --sources /etc/apt/sources.list.d/tailscale.list --packages tailscale \
               --mounts /var/lib/tailscale

With `/var/lib/tailscale` bound, the box keeps its tailnet identity and login. `setup.sh --check`
warns when DGX OS runs Tailscale and the options leave it out. A box reachable only over the tailnet
cannot be reached on a trial boot without Tailscale, and reverts after 10 minutes.

### vLLM

On the 64k kernel, CUDA hangs when it copies from a file-backed `mmap` to the GPU. vLLM's default
safetensors loader makes that copy for every tensor in the memory-mapped checkpoint. On a test box
(driver 580.178.04), stock vLLM sat at shard 0 for over 13 minutes, with libcuda spin-waiting and
the GPU idle. Reading the shards into ordinary memory first loaded the same model in 1.4 s. The 4k
kernel takes the same path without hanging, at its usual slower speed.

On the 64k flavour, start vLLM with `--safetensors-load-strategy eager`. Other loaders must copy
tensors into anonymous memory, for example with `tensor.clone()`, before moving them to the GPU.

## Site layer

Anything specific to one fleet goes in a site layer, so fleet addresses stay out of this repository.
`install --site DIR`, or `/etc/kindling-spark-os/site` on the DGX OS root when it exists:

- `DIR/root/` is copied over the image (units, scripts, config)
- `DIR/packages.txt` lists extra packages
- `DIR/customize.d/*.sh` run inside the image after the base customize step, in order. A site's
  scripts typically enable its units and add `/etc/fstab` lines, such as more binds from
  `/run/sparkos/host`.

## Services

| Unit | What |
|---|---|
| `mentatd` | mentat node daemon (needs `/etc/spark/node.env`) |
| `spark-agent` | status page and MCP tools on :8090, runs as `spark-agent` with `CAP_SYS_PTRACE` only (settings in `/etc/spark/agent.env`, optional) |
| `dispramd` | display carveout lender, on the validated driver only |
| `spark-watchdog` | pets the SBSA watchdog while the box can still fork |
| `spark-dmesg-snapshot.timer` | writes dmesg, docker and systemd state to `/var/log/spark` for the agent each minute |
| `spark-console` | cluster status on tty1; Esc to log in |
| `sparkos-trial-revert.timer` | reverts an unconfirmed trial boot |

## dispram

`dispramd` lends the GPU's 2 GiB display carveout to CUDA processes as ordinary device memory. The
driver never uses that memory on GB10. A patch for vLLM, and a plugin for stock images, put the tail of the KV
cache there. See [dispram/README.md](dispram/README.md). The server side is AGPL-3.0. The client,
vLLM plugin and patch are GPL-3.0 with a bundling exception, so they can ship inside vLLM or any image.

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
  and others): proprietary, with no right to redistribute. The setup image does not carry these packages.
  `install` fetches them from NVIDIA's repository with the box's own apt keyring. A built image
  contains them, so do not publish one.
- `dispram/rmlist.c` compiles against NVIDIA's open-gpu-kernel-modules headers (MIT).
- Everything else comes from Ubuntu under its usual licenses.

## Credits

The members of [kindlingai](https://github.com/kindlingai) make kindling-spark-os:

- [@mmastrac](https://github.com/mmastrac) (Matt Mastracci)
- [@coffee-the-dev](https://github.com/coffee-the-dev) (Steve)
- [@adapt-ai-systems](https://github.com/adapt-ai-systems) (Chuck)

Thanks to [@joesinvestments](https://github.com/joesinvestments) (Joey) for testing and lots of early
feedback.

The idea behind [dispram](dispram/) was independently discovered a few weeks before us by emihuang on
the Nvidia forums.
