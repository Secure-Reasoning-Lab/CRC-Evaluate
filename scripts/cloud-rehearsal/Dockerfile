FROM docker:29-dind

ENV DOCKER_TLS_CERTDIR=""

RUN apk add --no-cache \
    bash \
    coreutils \
    curl \
    git \
    openssh-client \
    py3-pip \
    py3-yaml \
    python3 \
    rsync \
    tar

COPY dind-entrypoint.sh /usr/local/bin/crsbench-dind-entrypoint.sh

ENTRYPOINT ["crsbench-dind-entrypoint.sh"]
CMD ["/bin/sh"]
