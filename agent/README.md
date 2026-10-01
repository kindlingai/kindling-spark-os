# agent

The spark host agent: one per box. It serves a status page and MCP tools on `:8090`, and registers
them with the box's mentatd as the group `agent-<hostname>`, so clients reach any box's tools
through the mentat router with `__group=agent-<hostname>`.

- `spark-agent.py`: the agent. It observes and never changes the host. It runs as its own user with
  `CAP_SYS_PTRACE` alone, so py-spy can read an engine's Python stacks in any container. What it
  cannot read directly comes from the files `spark-log-snapshot.sh` writes under `/var/log/spark`.
- `spark-memory.py`: accounts for every byte of host memory, including the GPU's share, which
  process RSS does not show. The agent and the snapshot timer both run it.

It needs no settings. The page and the `models` tool read every node and model from the box's own
mentatd, and the agent answers loopback and the box's own subnets, leaving out container bridges.
The optional `/etc/spark/agent.env` can name the router (`MENTAT_ROUTER_URL`) and replace the
subnets (`ALLOWED_SOURCES`).

The image installs them at `/opt/spark-agent/` and `/usr/local/bin/`, and runs the agent from
`spark-agent.service`. Beyond the standard library it needs py-spy and mentat's `ray.register`
shim, which the Dockerfile pins.

This code came from [mmastrac/spark-agent](https://github.com/mmastrac/spark-agent) at 8ee80bf,
whose `agent/` builds it as a container instead. That repository keeps `vllm/`, the status server
that model recipes take as a submodule.
