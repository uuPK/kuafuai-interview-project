FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash coreutils ripgrep \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
WORKDIR /work
ENV LANG=C.UTF-8
ENTRYPOINT ["/bin/bash", "--noprofile", "--norc"]
