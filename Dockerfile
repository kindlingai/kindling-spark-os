# The setup image for one kernel/driver pair (see stacks.yaml). It carries everything needed to build
# and install the OS image on a GB10 box except NVIDIA's DGX tuning packages, which setup takes from
# NVIDIA's repository with the box's own apt keyring.
#
#   tools/build-image.sh [STACK]
ARG MENTAT_VERSION=0.17.1
FROM mmastrac/mentatd:${MENTAT_VERSION} AS mentatd
FROM mmastrac/mentat-artifacts:${MENTAT_VERSION} AS mentat

# The pair's kernel and driver .debs, checked against stacks.yaml and indexed as an apt repo.
FROM ubuntu:24.04 AS stack
ARG STACK=default
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-yaml dpkg-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY stacks.yaml tools/fetch-stack.py /src/
RUN python3 /src/fetch-stack.py /src/stacks.yaml "$STACK" /stack

FROM ubuntu:24.04 AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev git ca-certificates python3-pip \
    && rm -rf /var/lib/apt/lists/*
# librmlist builds against the RM API headers of the stack's exact driver release: the structures it
# passes to the driver are not stable across releases.
COPY --from=stack /stack/stack.env /stack.env
COPY dispram/rmlist.c /src/
RUN . /stack.env \
    && git clone -q --depth 1 --branch "$DRIVER_UPSTREAM" --filter=blob:none --sparse \
         https://github.com/NVIDIA/open-gpu-kernel-modules.git /ogkm \
    && git -C /ogkm sparse-checkout set src/common/sdk/nvidia/inc kernel-open/common/inc \
         src/nvidia/arch/nvalloc/unix/include \
    && mkdir -p /out \
    && gcc -O2 -Wall -Werror -shared -fPIC /src/rmlist.c -o /out/librmlist.so \
         -I/ogkm/src/common/sdk/nvidia/inc -I/ogkm/kernel-open/common/inc \
         -I/ogkm/src/nvidia/arch/nvalloc/unix/include
# The spark agent, and the two things it needs beyond the stdlib: py-spy, and mentat's ray shim for
# ray.register. The shim installs as `ray`, so --no-deps keeps real ray out.
ARG SPARK_AGENT_REF=8ee80bf4b4584d548235bfe23b1ddfecfa0c361d
RUN git clone -q https://github.com/mmastrac/spark-agent.git /spark-agent \
    && git -C /spark-agent checkout -q "$SPARK_AGENT_REF"
COPY --from=mentat /out/ /mentat-out/
RUN pip install --break-system-packages --no-cache-dir --no-deps --target /out/agent-lib \
      py-spy /mentat-out/mentatd-*-py3-none-any.whl \
    && PYTHONPATH=/out/agent-lib python3 -c "from ray import register; register.connect" \
    && /out/agent-lib/bin/py-spy --version

FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
      mmdebstrap erofs-utils dpkg-dev python3 python3-yaml ca-certificates gnupg zstd util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY --from=stack /stack /opt/kindling/stack
COPY --from=mentatd /usr/local/bin/mentatd /usr/local/bin/mentatd-probe-machine /opt/kindling/mentatd/
COPY --from=build /out/librmlist.so /opt/kindling/dispram/
COPY --from=build /out/agent-lib /opt/kindling/agent/lib
COPY --from=build /spark-agent/agent/spark-agent.py /spark-agent/agent/spark-memory.py /opt/kindling/agent/
COPY dispram/dispramd.py dispram/LICENSE dispram/LICENSE-GPL dispram/BUNDLING-EXCEPTION dispram/README.md /opt/kindling/dispram/
COPY dispram/python /opt/kindling/dispram/python
COPY dispram/vllm /opt/kindling/dispram/vllm
COPY overlay /opt/kindling/overlay
COPY setup /opt/kindling/setup
COPY stacks.yaml LICENSE README.md /opt/kindling/
ENTRYPOINT ["/opt/kindling/setup/kindling-setup"]
CMD ["--help"]
