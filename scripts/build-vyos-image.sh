#!/usr/bin/env bash
# VyOS rolling の ISO から Docker イメージ home-router/vyos:rolling を作る。
#
#   scripts/build-vyos-image.sh
#
# 版は vyos/VERSION の 1 か所で固定する（ISO のリリース名・sha256、iso-to-oci のコミット）。
# 手元に必要なのは docker だけ。ISO の取得と展開は道具箱のコンテナ
# （scripts/vyos-iso-to-oci.Dockerfile）の中で行う。
#
# 環境変数
#   VYOS_IMAGE_CACHE_DIR  rootfs tar の置き場（既定: ~/.cache/home-router/vyos-image）。
#                         既に tar があれば ISO の取得と変換を飛ばして docker import だけ行う。
#                         GitHub Actions では .github/actions/vyos-image がここをキャッシュする。
#   VYOS_VERSION_FILE     版を固定したファイル（既定: vyos/VERSION）
#   VYOS_IMAGE_TAG        作るイメージのタグ（既定: home-router/vyos:rolling。後続の containerlab との契約）
#
# 注意: vyos-nightly-build のリリースは直近 30 本程度しか残らず、古いものは消える。
# 取得できないときは vyos/VERSION を新しいリリースに上げる（sha256 も更新する）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "${VYOS_VERSION_FILE:-${ROOT}/vyos/VERSION}"

: "${VYOS_VERSION:?vyos/VERSION に VYOS_VERSION が無い}"
: "${VYOS_ISO_SHA256:?vyos/VERSION に VYOS_ISO_SHA256 が無い}"
: "${VYOS_BUILD_REF:?vyos/VERSION に VYOS_BUILD_REF が無い}"
: "${ISO_TO_OCI_SHA256:?vyos/VERSION に ISO_TO_OCI_SHA256 が無い}"

TAG="${VYOS_IMAGE_TAG:-home-router/vyos:rolling}"
CACHE_DIR="${VYOS_IMAGE_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/home-router/vyos-image}"
ARCH=amd64
ISO_NAME="vyos-${VYOS_VERSION}-generic-${ARCH}.iso"
ISO_URL="https://github.com/vyos/vyos-nightly-build/releases/download/${VYOS_VERSION}/${ISO_NAME}"
TAR_NAME="vyos-${VYOS_VERSION}-oci-${ARCH}.tar.xz"
TOOLBOX="home-router/vyos-iso-to-oci:${VYOS_BUILD_REF:0:12}"

log() { echo "build-vyos-image: $*" >&2; }
die() { echo "build-vyos-image: エラー: $*" >&2; exit 1; }

mkdir -p "${CACHE_DIR}"
CACHE_DIR="$(cd "${CACHE_DIR}" && pwd)"

if [[ -s "${CACHE_DIR}/${TAR_NAME}" ]]; then
  log "キャッシュの rootfs tar を使う: ${CACHE_DIR}/${TAR_NAME}"
else
  log "道具箱のイメージを作る: ${TOOLBOX}"
  docker build --quiet \
    --build-arg "VYOS_BUILD_REF=${VYOS_BUILD_REF}" \
    --build-arg "ISO_TO_OCI_SHA256=${ISO_TO_OCI_SHA256}" \
    -t "${TOOLBOX}" -f "${ROOT}/scripts/vyos-iso-to-oci.Dockerfile" "${ROOT}/scripts" >/dev/null

  log "ISO の有無を確かめる: ${ISO_URL}"
  status="$(docker run --rm "${TOOLBOX}" curl -sSL -o /dev/null -w '%{http_code}' -I "${ISO_URL}" || true)"
  if [[ "${status}" != "200" ]]; then
    die "ISO を取得できない（HTTP ${status:-なし}）: ${ISO_URL}
  vyos-nightly-build のリリースは直近 30 本程度しか残らず、古いものは削除される。
  https://github.com/vyos/vyos-nightly-build/releases から 2026-07 以降の rolling を選び、
  vyos/VERSION の VYOS_VERSION と VYOS_ISO_SHA256 を更新すること。"
  fi

  log "ISO を取得して OCI の rootfs tar に変換する（数分かかる）"
  # 一時ファイルは CACHE_DIR の中に作り、成功した tar だけを残す
  docker run --rm -v "${CACHE_DIR}:/work" \
    -e ISO_URL="${ISO_URL}" -e ISO_NAME="${ISO_NAME}" -e ISO_SHA256="${VYOS_ISO_SHA256}" \
    -e TAR_NAME="${TAR_NAME}" -e ARCH="${ARCH}" \
    "${TOOLBOX}" bash -euo pipefail -c '
      trap "rm -rf /work/tmp-build" EXIT
      mkdir -p /work/tmp-build && cd /work/tmp-build
      curl -fsSL --retry 3 -o "${ISO_NAME}" "${ISO_URL}"
      echo "${ISO_SHA256}  ${ISO_NAME}" | sha256sum -c -
      iso-to-oci "${ISO_NAME}"
      produced=(vyos-*-oci-"${ARCH}".tar.xz)
      [[ -s "${produced[0]}" ]] || { echo "iso-to-oci が tar を出さなかった" >&2; exit 1; }
      mv "${produced[0]}" "/work/${TAR_NAME}.part"
      mv "/work/${TAR_NAME}.part" "/work/${TAR_NAME}"
    '
fi

log "docker import: ${TAG}"
# CMD と HEALTHCHECK は iso-to-oci の案内（containerlab の vyosnetworks_vyos と同じ）に合わせる
docker import --platform "linux/${ARCH}" \
  --change 'CMD ["/sbin/init"]' \
  --change 'HEALTHCHECK --start-period=10s CMD systemctl is-system-running' \
  --change "LABEL org.opencontainers.image.version=${VYOS_VERSION}" \
  "${CACHE_DIR}/${TAR_NAME}" "${TAG}" >/dev/null

log "完了: ${TAG}（VyOS ${VYOS_VERSION}）"
