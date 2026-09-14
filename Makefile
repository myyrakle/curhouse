# curhouse — 개발 · 이미지 배포 진입점
#
# 자주 쓰는 것:
#   make test              — pytest 실행
#   make image             — 로컬 아키만 (개발용, push 안 함)
#   make image-publish     — 멀티아키 빌드 후 ghcr.io 로 push
#
# 커스터마이즈:
#   make image-publish TAG=v1.2.3
#   make image-publish OWNER=myfork TAG=latest
#   PLATFORMS=linux/amd64 make image-publish   # arm64 스킵

OWNER     ?= myyrakle
IMAGE     ?= ghcr.io/$(OWNER)/curhouse
TAG       ?= latest
PLATFORMS ?= linux/amd64,linux/arm64
BUILDER   ?= curhouse-builder

.PHONY: help
help:
	@awk 'BEGIN{FS=":.*##"} /^[a-z][a-zA-Z0-9_-]+:.*##/ {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: test
test: ## pytest 전체 실행
	uv run pytest

.PHONY: image
image: ## 로컬 아키로만 빌드 (push 안 함, 빠른 스모크 테스트용)
	docker build -f deploy/Dockerfile -t $(IMAGE):$(TAG) .

.PHONY: buildx-ready
buildx-ready:
	@docker buildx inspect $(BUILDER) >/dev/null 2>&1 || \
	  docker buildx create --name $(BUILDER) --use --bootstrap
	@docker buildx use $(BUILDER)

.PHONY: image-publish
image-publish: buildx-ready ## 멀티아키 빌드 + ghcr.io push (사전에 `docker login ghcr.io` 필요)
	docker buildx build \
	  --platform $(PLATFORMS) \
	  --file deploy/Dockerfile \
	  --tag $(IMAGE):$(TAG) \
	  --push \
	  .
	@echo
	@echo "✓ Pushed $(IMAGE):$(TAG)"
	@echo "  Pull:  docker pull $(IMAGE):$(TAG)"

.PHONY: image-tag-sha
image-tag-sha: ## 현재 커밋 SHA(짧은) 로 태그 하나 더 push (image-publish 후 실행 권장)
	$(eval SHA := $(shell git rev-parse --short HEAD))
	docker buildx build \
	  --platform $(PLATFORMS) \
	  --file deploy/Dockerfile \
	  --tag $(IMAGE):sha-$(SHA) \
	  --push \
	  .
	@echo "✓ Also pushed $(IMAGE):sha-$(SHA)"

.PHONY: clean-buildx
clean-buildx: ## buildx builder 제거 (문제 있을 때만)
	-docker buildx rm $(BUILDER)
