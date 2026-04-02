#!/bin/sh
set -eu

start-docker.sh >/var/log/dockerd.log 2>&1 &

tries=0
until docker info >/dev/null 2>&1; do
  tries=$((tries + 1))
  if [ "$tries" -ge 60 ]; then
    echo "Docker daemon did not become ready inside rehearsal container" >&2
    exit 1
  fi
  sleep 1
done

exec "$@"
