#!/usr/bin/env bash
# 로컬에서 멀티아키 이미지를 ghcr.io 로 밀어 올리는 원-샷 스크립트.
# CI 가 아닌 손으로 급하게 릴리스 찍어야 할 때 사용.
#
# 사전 조건:
#   - docker buildx 사용 가능 (Docker Desktop 24+ 또는 buildx 플러그인 설치)
#   - ghcr.io 에 로그인돼 있어야 함:
#       echo $GITHUB_TOKEN | docker login ghcr.io -u <github-user> --password-stdin
#     (토큰은 GitHub → Settings → Developer settings → PAT (classic) → write:packages)
#
# 사용법:
#   scripts/publish-image.sh <tag>
#   scripts/publish-image.sh v1.2.3
#   scripts/publish-image.sh latest
#
# 환경변수 오버라이드:
#   OWNER      기본 "myyrakle"  (ghcr.io/<OWNER>/curhouse)
#   PLATFORMS  기본 "linux/amd64,linux/arm64"
set -euo pipefail

TAG="${1:?usage: $0 <tag>  (예: v1.2.3, latest)}"
OWNER="${OWNER:-myyrakle}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
IMAGE="ghcr.io/${OWNER}/curhouse:${TAG}"

# 리포지토리 루트에서 실행 (Dockerfile 이 deploy/ 하위, 컨텍스트는 루트)
cd "$(dirname "$0")/.."

# builder 인스턴스 준비 (한 번 만들어두면 재사용됨)
if ! docker buildx inspect curhouse-builder >/dev/null 2>&1; then
  docker buildx create --name curhouse-builder --use --bootstrap
else
  docker buildx use curhouse-builder
fi

echo "Building ${IMAGE} for ${PLATFORMS} ..."
docker buildx build \
  --platform "${PLATFORMS}" \
  --file deploy/Dockerfile \
  --tag "${IMAGE}" \
  --push \
  .

echo
echo "✓ Pushed ${IMAGE}"
echo "  Pull:  docker pull ${IMAGE}"
