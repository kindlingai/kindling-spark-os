#!/bin/sh
# Snapshot host-only diagnostics into the MCP-readable log directory.
#
# What the container cannot read for itself:
#   dmesg  - kernel.dmesg_restrict=1 and the container has no CAP_SYSLOG
#   docker - would need the Docker socket, which is root-equivalent on the host
#            and a bad thing to expose behind an unauthenticated endpoint
#   systemd- the agent image carries no systemctl, and giving it one would mean
#            mounting the host's /run/systemd, which is a control channel and
#            not only a read of it
#
# So the host collects, and the container only ever reads ordinary files.
# Every write is tail-bounded and atomic, so a reader always sees a whole file
# and this stays cheap enough to run every minute.
set -u
LOGDIR=${SPARK_LOG_DIR:-/var/log/spark}
TAIL=${TAIL:-2000}
mkdir -p "$LOGDIR"

atomic() {   # atomic <destination>; content on stdin
  cat > "$1.tmp" && mv -f "$1.tmp" "$1" && chmod 644 "$1"
}

dmesg -T 2>/dev/null | atomic "$LOGDIR/dmesg.log"

# Container inventory, so the agent can answer "what is running" without the
# Docker socket. The socket is root-equivalent on the host, and the agent
# answers an endpoint a model talks to.
docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' \
  2>/dev/null | atomic "$LOGDIR/docker-ps.log"

# Running services. The agent reported "systemctl is not installed" for every
# node before this existed, because its image is python:3.12-slim and systemd
# is not something a container should be handed.
{
  date -u "+# %Y-%m-%dT%H:%M:%SZ"
  systemctl list-units --type=service --state=running --no-pager --no-legend \
    2>&1 || echo "(systemctl failed)"
} | atomic "$LOGDIR/systemd-units.log"

# --tail bounds the read: docker seeks the end rather than streaming the whole
# file, so this costs the same whether the log is 1 MB or 1 GB.
for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
  docker logs --tail "$TAIL" --timestamps "$c" 2>&1 | atomic "$LOGDIR/docker-$c.log"
done

# NVIDIA driver faults. dmesg.log already holds these, buried in 2000 lines
# nobody reads; this is the same data at a size the agent can put on a status
# page. NV_ERR_NO_MEMORY ran for 23 minutes before the GPU_MEM_UTIL=0.90 wedge
# on 2026-08-27 while the model served normally and every other signal looked
# healthy. Xid is the driver's other fault channel and costs nothing to catch.
# NVRM alone would match the module-load banner every node prints at boot,
# which would leave the indicator red on a healthy machine and teach everyone
# to ignore it. Faults carry an NV_ERR_ code, an Xid, or say so in words.
_nv=$(dmesg -T 2>/dev/null |
      grep -aE 'NV_ERR_|Xid[ (:]|NVRM:.*([Ff]ail|[Ee]rror|[Tt]imeout|[Ff]ault)')
{
  date -u "+# %Y-%m-%dT%H:%M:%SZ"
  echo "# matched $(printf '%s\n' "$_nv" | grep -c .)"
  printf '%s\n' "$_nv" | tail -200
} | atomic "$LOGDIR/driver-errors.log"

# Host memory, broken down by role. The GPU shares this memory and its
# allocations skip process RSS, so ps accounts for about 6 GiB of the 121 GiB
# on a serving node. Only the host can do this: /proc inside the container
# shows container processes alone.
if [ -x /usr/local/bin/spark-memory.py ]; then
  { date -u "+# %Y-%m-%dT%H:%M:%SZ"; /usr/local/bin/spark-memory.py -v 2>&1; } \
    | atomic "$LOGDIR/memory.log"
fi

# Memory over time, one line a minute. memory.log is a snapshot and answers
# "what is it now"; this answers "what has it been doing", which is the
# question a slow allocator leak needs and the one we could not answer when
# gx10-2353 wedged on 2026-08-28 with the engine idle at 15% KV usage.
# Bounded to a week so it cannot fill the disk on its own.
{
  awk '/^MemTotal|^MemFree|^MemAvailable|^Cached:|^Slab:|^PageTables/ {
        printf "%s=%d ", substr($1, 1, length($1)-1), $2/1024 }' /proc/meminfo
  echo "date=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$LOGDIR/memory-history.log"
tail -n 10080 "$LOGDIR/memory-history.log" > "$LOGDIR/memory-history.tmp" \
  && mv -f "$LOGDIR/memory-history.tmp" "$LOGDIR/memory-history.log" \
  && chmod 644 "$LOGDIR/memory-history.log"

# Interconnect. Rates come from two reads a second apart rather than lifetime
# totals, because a total cannot say whether the link is busy now. Ruling the
# network in or out was the first question asked of three separate stalls.
{
  date -u "+# %Y-%m-%dT%H:%M:%SZ"
  for d in /sys/class/infiniband/*/ports/1; do
    [ -d "$d" ] || continue
    dev=$(basename "$(dirname "$(dirname "$d")")")
    tx1=$(cat "$d/counters/port_xmit_data" 2>/dev/null) || continue
    rx1=$(cat "$d/counters/port_rcv_data" 2>/dev/null)
    [ "${tx1:-0}" -eq 0 ] 2>/dev/null && continue      # a down port reports zeros
    sleep 1
    tx2=$(cat "$d/counters/port_xmit_data"); rx2=$(cat "$d/counters/port_rcv_data")
    # port_*_data counts 4-byte words, per the IB spec.
    echo "$dev tx $(( (tx2-tx1)*4/1000000 )) MB/s  rx $(( (rx2-rx1)*4/1000000 )) MB/s"
    for c in local_ack_timeout_err roce_adp_retrans packet_seq_err out_of_sequence \
             out_of_buffer np_ecn_marked_roce_packets; do
      v=$(cat "$d/hw_counters/$c" 2>/dev/null) || continue
      [ "${v:-0}" -gt 0 ] 2>/dev/null && echo "  $c = $v"
    done
    echo "  port_xmit_wait = $(cat "$d/counters/port_xmit_wait" 2>/dev/null)"
  done
} | atomic "$LOGDIR/interconnect.log"
