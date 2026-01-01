#!/bin/bash
set -ex

uv run crsbench patch-verify benchmarks/sanity-mock-c-delta-01 --force-rebuild
uv run crsbench patch-verify benchmarks/afc-libxml2-full-01 --force-rebuild
uv run crsbench patch-verify benchmarks/sanity-mock-java-delta-01 --force-rebuild
uv run crsbench patch-verify benchmarks/afc-apache-commons-compress-delta-01 --force-rebuild