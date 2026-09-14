# Kubernetes 샘플

CronJob 하나로 데일리 CUR sync 만 돌리는 최소 구성. ClickHouse 는 클러스터 밖(SaaS 또는 별도 노드) 를 가정.

## 파일 구성

| 파일 | 용도 |
|---|---|
| `namespace.yaml`   | `curhouse` 네임스페이스 |
| `configmap.yaml`   | `config.toml` (bucket/account/[[sources]]/CH 접속) |
| `secret.yaml.example` | AWS 키 + CH 비밀번호 (실제로는 sealed-secrets/SOPS 권장) |
| `pvc.yaml`         | state 파일(`state.json`) 영속화 (100Mi) |
| `cronjob.yaml`     | 매일 06:00 UTC `curhouse sync` 실행 |

## 배포 순서

```bash
kubectl apply -f namespace.yaml

# secret 은 예시 복사 → 실제 값 채우고 별도 이름/파일로 apply
cp secret.yaml.example secret.yaml
$EDITOR secret.yaml
kubectl apply -f secret.yaml

kubectl apply -f configmap.yaml
kubectl apply -f pvc.yaml
kubectl apply -f cronjob.yaml
```

## 즉시 한 번 실행 (스케줄 안 기다리고)

```bash
kubectl -n curhouse create job --from=cronjob/curhouse-sync curhouse-sync-manual-$(date +%s)
kubectl -n curhouse logs -f -l job-name=curhouse-sync-manual-...
```

## 자주 쓰는 조회

**상태 확인:**
```bash
kubectl -n curhouse get cronjob curhouse-sync
kubectl -n curhouse get jobs
kubectl -n curhouse logs job/<이름> -f
```

**특정 소스만 강제 재로드:**
```bash
kubectl -n curhouse run curhouse-once --rm -it --restart=Never \
  --image=ghcr.io/myyrakle/curhouse:latest \
  --overrides='{"spec":{"volumes":[{"name":"config","configMap":{"name":"curhouse-config"}},{"name":"state","persistentVolumeClaim":{"claimName":"curhouse-state"}}],"containers":[{"name":"curhouse-once","image":"ghcr.io/myyrakle/curhouse:latest","command":["curhouse","sync","--only-source","qa"],"envFrom":[{"secretRef":{"name":"curhouse-secrets"}}],"env":[{"name":"CURHOUSE_STATE_PATH","value":"/state/state.json"}],"volumeMounts":[{"name":"config","mountPath":"/app/config.toml","subPath":"config.toml"},{"name":"state","mountPath":"/state"}]}]}}'
```
(더 자주 쓸 것 같으면 별도 Job 매니페스트로 뽑는 게 낫다.)

## 리소스 튜닝 팁

- CUR가 ~140 컬럼 와이드 스키마라 INSERT 가 수백 MB~수 GB 메모리를 요구.
  cronjob 의 `resources.limits.memory: 2Gi` 는 초기값 — 파티션이 커지면 상향.
- 여러 소스를 동시에 sync 하지 않도록 `concurrencyPolicy: Forbid` 유지.
- 이미지는 프로덕션에선 `latest` 대신 태그 고정:
  ```yaml
  image: ghcr.io/myyrakle/curhouse:v1.2.3
  ```
  새 이미지 배포는 리포지토리 루트에서 `make image-publish TAG=v1.2.3` (사전에 `docker login ghcr.io`).

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

## Metabase 도 같이 띄우려면?

이 샘플은 sync 만 다룬다. Metabase 는 별도 헬름 차트(예: `pmint93/metabase`) 또는 자체 매니페스트로 배포하고, ClickHouse 데이터소스로 이 sync 가 채우는 테이블(`aws_billing.cur_line_items`)을 등록.
