# Kubernetes 배포 샘플

CUR sync + Metabase + Metabase 앱DB(Postgres) 세트를 한 네임스페이스에 올리는 최소 구성. ClickHouse 는 외부(SaaS 또는 별도 클러스터) 를 가정.

이미지는 **public** — `ghcr.io/myyrakle/curhouse` 는 익명 pull 가능하므로 `imagePullSecrets` 필요 없음.

## 파일 구성

| 파일 | 용도 |
|---|---|
| `namespace.yaml`          | `curhouse` 네임스페이스 |
| `configmap.yaml`          | `config.toml` (buckets/sources/CH 접속) |
| `secret.yaml.example`     | AWS 키 + AWS/CUR 식별정보 + CH 비밀번호 (실제 값은 SOPS/sealed-secrets 권장) |
| `pvc-curhouse-state.yaml` | curhouse sync state(`state.json`) 영속화 |
| `pvc-postgres.yaml`       | Metabase 앱DB 영속화 |
| `postgres.yaml`           | Postgres 16-alpine StatefulSet + headless Service |
| `postgres-migrate-job.yaml` | 기존 docker 볼륨 → k8s PVC 이전용 임시 Job |
| `metabase.yaml`           | Metabase Deployment + Service |
| `cronjob-sync.yaml`       | 매일 06:00 UTC `curhouse sync` |

## 라벨 규칙

모든 리소스에 recommended labels:
- `app.kubernetes.io/name: curhouse`
- `app.kubernetes.io/part-of: curhouse`
- `app.kubernetes.io/component: sync|metabase|postgres|state|config|secrets`
- `app.kubernetes.io/version: v0.1.0` (CronJob·이미지 태그 변경 시)
- `app.kubernetes.io/managed-by: kubectl`

Pod 라벨엔 `app: <name>` 도 병기 — Loki/Fluent Bit 같은 옛 로그 수집기가 이 키를 스크레이프. 로그 필터/대시보드에서:
```logql
{app="curhouse-sync"} |= "billing period"
{namespace="curhouse", app_kubernetes_io_component="metabase"} |= "ERROR"
```

## 배포 순서

**1) 기본 리소스**
```bash
kubectl apply -f namespace.yaml

# Secret 은 예시 복사 → 실제 값 채우고 별도 파일로 apply
cp secret.yaml.example secret.yaml
$EDITOR secret.yaml
kubectl apply -f secret.yaml

$EDITOR configmap.yaml    # config.toml 안의 [[sources]] · CH host 등 실제 값으로
kubectl apply -f configmap.yaml
kubectl apply -f pvc-curhouse-state.yaml
kubectl apply -f pvc-postgres.yaml
```

**2) Postgres 볼륨 이전 (기존 docker 배포 이력 있는 경우만)**
[아래 "Postgres 볼륨 이전" 섹션](#postgres-볼륨-이전) 참조.

새로 시작하는 클러스터면 이 단계 스킵.

**3) Postgres + Metabase 기동**
```bash
kubectl apply -f postgres.yaml
kubectl -n curhouse rollout status statefulset/curhouse-postgres
kubectl apply -f metabase.yaml
kubectl -n curhouse rollout status deploy/curhouse-metabase
```

**4) CronJob 등록**
```bash
kubectl apply -f cronjob-sync.yaml
```

**5) 즉시 한 번 sync**
```bash
kubectl -n curhouse create job --from=cronjob/curhouse-sync curhouse-sync-manual-$(date +%s)
kubectl -n curhouse logs -f -l job-name=curhouse-sync-manual-...
```

## Postgres 볼륨 이전

기존 docker-compose 로 돌리던 Metabase 앱DB(`metabase-pg-data` 볼륨)를 k8s PVC 로 그대로 옮긴다.

**A. docker host 에서 dump 만들기**
```bash
# 1) 안전하게 docker Metabase 중지 (앱DB에 write 없게)
docker compose -f deploy/docker-compose.yml stop metabase metabase-provision curhouse-sync curhouse-sync-init

# 2) 실행 중인 postgres 컨테이너에서 데이터 폴더 tar
#    (컨테이너 안의 PGDATA 를 그대로 뜨는 게 가장 확실)
docker run --rm \
  -v curhouse_metabase-pg-data:/src:ro \
  -v "$PWD":/out \
  alpine:3 \
  tar czf /out/metabase-pg-data.tar.gz -C /src .

# 3) 검증
ls -lh metabase-pg-data.tar.gz
```

볼륨 이름 확인: `docker volume ls | grep pg-data`. compose 프로젝트 접두어(`curhouse_`, `deploy_` 등)가 붙을 수 있음.

**B. k8s 로 옮기기**
```bash
# 1) 이전 job 띄우기 (한 시간 동안 sleep 한 채 PVC 마운트만 하고 대기)
kubectl apply -f postgres-migrate-job.yaml

# 2) Pod 이름 잡기
POD=$(kubectl -n curhouse get pod \
  -l app.kubernetes.io/component=postgres-migrate \
  -o jsonpath='{.items[0].metadata.name}')

# 3) tarball 을 그 pod 에 복사
kubectl -n curhouse cp ./metabase-pg-data.tar.gz $POD:/tmp/data.tar.gz

# 4) 마운트된 PVC 안에 풀기
kubectl -n curhouse exec $POD -- sh -c \
  'cd /var/lib/postgresql/data && rm -rf ./* && tar xzf /tmp/data.tar.gz && chown -R 999:999 .'

# 5) 확인 (PG_VERSION 파일 있으면 정상)
kubectl -n curhouse exec $POD -- ls /var/lib/postgresql/data

# 6) 이전 Job 삭제
kubectl -n curhouse delete job curhouse-postgres-migrate
```

**주의:**
- postgres 16-alpine 이미지의 PGDATA 는 기본적으로 `/var/lib/postgresql/data` 안에 그대로 있음. `PGDATA` env 를 `/var/lib/postgresql/data/pgdata` 로 설정했지만, tar 를 어디에 풀지에 유의. 위 스크립트는 볼륨 루트에 풀기 때문에 postgres.yaml 의 PGDATA 도 `/var/lib/postgresql/data` 로 맞추는 게 안전.
- **원본 볼륨의 PostgreSQL 메이저 버전이 16 이 아니면** 여기서 실패. 이 경우 docker 에서 `pg_dumpall` → k8s postgres 에 `psql` 로 import 하는 논리 백업 경로로 우회.

정 안 될 것 같으면 A안 대신 논리 백업:
```bash
docker exec curhouse-metabase-db pg_dumpall -U metabase > metabase-all.sql
# k8s postgres pod 에 파일 복사 후: psql -U metabase < metabase-all.sql
```

## 자주 쓰는 조회

```bash
# CronJob 상태
kubectl -n curhouse get cronjob curhouse-sync

# 최근 실행 로그
kubectl -n curhouse get jobs
kubectl -n curhouse logs -l app.kubernetes.io/component=sync --tail=200

# Metabase 로그
kubectl -n curhouse logs -l app.kubernetes.io/component=metabase --tail=200

# Postgres 접속 (임시)
kubectl -n curhouse exec -it statefulset/curhouse-postgres -- psql -U metabase
```

## 외부 노출

`metabase.yaml` 의 Service 는 기본 ClusterIP. 브라우저에서 접근하려면:

**개발/스테이징 — port-forward:**
```bash
kubectl -n curhouse port-forward svc/curhouse-metabase 3000:3000
# → http://localhost:3000
```

**프로덕션 — Ingress:**
클러스터의 ingress 컨트롤러에 맞게 별도 Ingress 리소스 추가. 예 (nginx):
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: curhouse-metabase
  namespace: curhouse
spec:
  rules:
    - host: metabase.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: curhouse-metabase
                port:
                  number: 3000
```

## 리소스 튜닝 팁

- **sync**: CUR가 ~140 컬럼 와이드 스키마라 INSERT 가 수백 MB~수 GB 메모리를 요구. cronjob 의 `resources.limits.memory: 2Gi` 는 초기값. 파티션이 커지면 상향.
- **metabase**: `JAVA_OPTS: -Xmx1g` 로 고정. 사용자 늘어나면 상향 + limits 조정.
- **postgres**: Metabase 앱DB 는 작음 (수백 MB). 기본값으로 충분.
- 여러 소스를 동시에 sync 하지 않도록 `concurrencyPolicy: Forbid` 유지.
- 이미지 태그는 프로덕션에선 `latest` 대신 `v0.1.0` 같은 고정 태그. Makefile 로 새 태그 배포:
  ```bash
  make image-publish TAG=v0.2.0
  # 그 후 cronjob-sync.yaml 이미지 태그 v0.2.0 으로 수정 + kubectl apply
  ```

## 원격 CH SSL 접속

`configmap.yaml` 의 config.toml 에서:
```toml
[clickhouse]
host = "clickhouse.example.com"
port = 443
user = "api"
secure = true
```
`CURHOUSE_CH_SECURE` env override 는 아직 없어서 SSL 여부는 config.toml 로 지정한다.

## 즉시 sync (다른 예시)

**특정 소스만 강제 재로드:**
```bash
kubectl -n curhouse create job manual-qa-$(date +%s) --from=cronjob/curhouse-sync -- \
  curhouse sync --only-source qa
```

**특정 월만:**
```bash
kubectl -n curhouse create job manual-aug-$(date +%s) --from=cronjob/curhouse-sync -- \
  curhouse sync --only-period 2026-08
```

## 다른 curhouse 태그로 이미지 업데이트

```bash
kubectl -n curhouse set image cronjob/curhouse-sync sync=ghcr.io/myyrakle/curhouse:v0.2.0
```
