# Docker Compose 샘플 (sync-only)

ghcr.io 이미지를 그대로 받아 데일리 CUR sync 만 돌리는 최소 구성. ClickHouse 는 외부(호스트 로컬 또는 원격) 를 가정.

Metabase 까지 한 스택으로 띄우고 싶다면 리포지토리 루트의 `deploy/docker-compose.yml` 을 참고.

## 셋업

```bash
cp config.toml.example config.toml         # bucket/account/[[sources]] 채우기
cp .env.example .env                       # AWS 키 + CH 접속 정보 채우기
```

## 실행

**데일리 cron 시작 (기본 06:00 UTC):**
```bash
docker compose up -d
docker compose logs -f sync
```

**지금 즉시 한 번만 sync:**
```bash
docker compose run --rm sync-once
```

## 자주 쓰는 옵션

**특정 소스만:**
```bash
docker compose run --rm sync-once curhouse sync --only-source qa
```

**특정 월만 강제 재로드:**
```bash
docker compose run --rm sync-once curhouse sync --only-period 2026-09
```

**현재 상태 조회:**
```bash
docker compose run --rm sync-once curhouse status
```

## 원격 ClickHouse (SSL) 겨냥

`.env` 에서 호스트 지정:
```
CURHOUSE_CH_HOST=clickhouse.example.com
CURHOUSE_CH_PORT=443
CURHOUSE_CH_USER=api
CURHOUSE_CH_PASSWORD=...
```

그리고 `config.toml` 의 `[clickhouse]` 섹션에서 `secure = true` 로. (`CURHOUSE_CH_SECURE` env override 는 아직 없어서 파일로 직접 지정.)

## 이미지 버전 고정

프로덕션에선 `latest` 대신 태그 고정 권장:
```yaml
image: ghcr.io/myyrakle/curhouse:v1.2.3
```

## 이미지 새로 빌드/배포하려면

리포지토리 루트에서:
```bash
docker login ghcr.io                # 최초 1회 (write:packages PAT 필요)
make image-publish TAG=v1.2.3       # 멀티아키 빌드 + push
```
자세한 옵션은 `make help`.
