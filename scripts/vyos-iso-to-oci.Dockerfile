# syntax=docker/dockerfile:1.7
# VyOS の ISO から OCI の rootfs tar を作る道具箱（scripts/build-vyos-image.sh が使う）。
# iso-to-oci は vyos/vyos-build の固定コミットから取り、sha256 で検証する（版は vyos/VERSION）。
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl jq squashfs-tools tar xorriso xz-utils \
 && rm -rf /var/lib/apt/lists/*

ARG VYOS_BUILD_REF
ARG ISO_TO_OCI_SHA256
ADD --checksum=sha256:${ISO_TO_OCI_SHA256} \
    https://raw.githubusercontent.com/vyos/vyos-build/${VYOS_BUILD_REF}/scripts/iso-to-oci \
    /usr/local/bin/iso-to-oci
RUN chmod 0755 /usr/local/bin/iso-to-oci

WORKDIR /work
