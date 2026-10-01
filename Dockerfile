# The setup image for one kernel/driver pair (see stacks.yaml). It carries everything needed to build
# and install the OS image on a GB10 box except NVIDIA's DGX tuning packages, which setup takes from
# NVIDIA's repository with the box's own apt keyring.
#
#   tools/build-image.sh [STACK]

# Every outside input is pinned: mentat by image digest (the multi-arch index, so it holds on any
# build host), open-gpu-kernel-modules by commit (stacks.yaml), py-spy by wheel hash. A tag can be moved; these cannot. The tags are there for readers.
ARG MENTATD_IMAGE=mmastrac/mentatd:0.18.0@sha256:f42bc5d1a2802ac5c1361c492ed27a0fe753e089ebc8657ddb6e5bc937f6d5f2
ARG MENTAT_ARTIFACTS_IMAGE=mmastrac/mentat-artifacts:0.18.0@sha256:7e317d0ed99cd9682da9176cf4bc3afeeb425b1fdf3b254f3c80be16dc0edd0c
FROM ${MENTATD_IMAGE} AS mentatd
FROM ${MENTAT_ARTIFACTS_IMAGE} AS mentat

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
    && git init -q /ogkm \
    && git -C /ogkm remote add origin https://github.com/NVIDIA/open-gpu-kernel-modules.git \
    && git -C /ogkm sparse-checkout set src/common/sdk/nvidia/inc kernel-open/common/inc \
         src/nvidia/arch/nvalloc/unix/include \
    && git -C /ogkm fetch -q --depth 1 --filter=blob:none origin "$OGKM_COMMIT" \
    && git -C /ogkm checkout -q FETCH_HEAD \
    && test "$(git -C /ogkm rev-parse HEAD)" = "$OGKM_COMMIT" \
    && mkdir -p /out \
    && gcc -O2 -Wall -Werror -shared -fPIC /src/rmlist.c -o /out/librmlist.so \
         -I/ogkm/src/common/sdk/nvidia/inc -I/ogkm/kernel-open/common/inc \
         -I/ogkm/src/nvidia/arch/nvalloc/unix/include
# The two things the spark agent (agent/) needs beyond the stdlib: py-spy, and mentat's ray shim for
# ray.register. The shim installs as `ray`, so --no-deps keeps real ray out.
COPY --from=mentat /out/ /mentat-out/
ARG PY_SPY_VERSION=0.4.2
ARG PY_SPY_SHA256=142887e984a4e541071c99a4401ff8c3770f255d329dbd0f64e8c1dd51882cce
RUN echo "py-spy==$PY_SPY_VERSION --hash=sha256:$PY_SPY_SHA256" > /tmp/py-spy.txt \
    && pip install --break-system-packages --no-cache-dir --no-deps --require-hashes \
         --only-binary :all: --target /out/agent-lib -r /tmp/py-spy.txt \
    && pip install --break-system-packages --no-cache-dir --no-deps --target /out/agent-lib \
         /mentat-out/mentatd-*-py3-none-any.whl \
    && PYTHONPATH=/out/agent-lib python3 -c "from ray import register; register.connect" \
    && /out/agent-lib/bin/py-spy --version

FROM ubuntu:24.04
LABEL org.opencontainers.image.source=https://github.com/kindlingai/kindling-spark-os
RUN apt-get update && apt-get install -y --no-install-recommends \
      mmdebstrap erofs-utils dpkg-dev python3 python3-yaml ca-certificates gnupg zstd util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY --from=stack /stack /opt/kindling/stack
COPY --from=mentatd /usr/local/bin/mentatd /usr/local/bin/mentatd-probe-machine /opt/kindling/mentatd/
COPY --from=build /out/librmlist.so /opt/kindling/dispram/
COPY --from=build /out/agent-lib /opt/kindling/agent/lib
COPY agent/spark-agent.py agent/spark-memory.py /opt/kindling/agent/
COPY dispram/dispramd.py dispram/LICENSE dispram/LICENSE-GPL dispram/BUNDLING-EXCEPTION dispram/README.md /opt/kindling/dispram/
COPY dispram/python /opt/kindling/dispram/python
COPY dispram/vllm /opt/kindling/dispram/vllm
COPY overlay /opt/kindling/overlay
COPY setup /opt/kindling/setup
COPY VERSION stacks.yaml LICENSE README.md /opt/kindling/
ENTRYPOINT ["/opt/kindling/setup/kindling-setup"]
CMD ["--help"]
