ARG REHEARSAL_BASE_IMAGE=cruizba/ubuntu-dind:noble-latest
FROM ${REHEARSAL_BASE_IMAGE}

ENV DOCKER_TLS_CERTDIR=""

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    coreutils \
    curl \
    git \
    openssh-client \
    python3 \
    python3-pip \
    python3-yaml \
    rsync \
    tar \
 && rm -rf /var/lib/apt/lists/*

COPY dind-entrypoint.sh /usr/local/bin/crsbench-dind-entrypoint.sh

ENTRYPOINT ["crsbench-dind-entrypoint.sh"]
CMD ["/bin/bash"]
