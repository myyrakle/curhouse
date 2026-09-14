#!/usr/bin/env python3
"""v1 → v2 마이그레이션: cur_line_items 파티션 스키마를 (_source, _billing_period)로 확장.

v1 스키마: PARTITION BY _billing_period, 컬럼에 _source 없음 (단일 소스 = dev)
v2 스키마: PARTITION BY (_source, _billing_period), _source LowCardinality(String) 추가

전략:
1. 백업: cur_line_items → cur_line_items_v1_backup (RENAME, 원자적)
2. 새 테이블 생성: 최신 매니페스트로 v2 DDL 로 cur_line_items 생성
3. 상태 초기화: state 파일의 dev 소스 매니페스트 제거 → 다음 sync 가 전 기간 재로드
4. 사용자가 `curhouse sync` 실행 → S3에서 dev 다시 로드 + qa/prod 최초 로드

트레이드오프:
- 마이그레이션 중~sync 완료까지 대시보드가 잠깐 빈다 (몇 분).
- 무중단이 필요하면 백업 테이블에서 INSERT SELECT 로 소급 이관하는 방식을
  써야 하는데, 5M 행 한 트랜잭션이 CH 메모리 상한을 건드릴 수 있어 이 스크립트는
  간단한 drop-and-resync 경로를 취한다.

사용법:
    CURHOUSE_CH_PASSWORD=... python scripts/migrate_v1_to_v2.py [--config PATH]

--dry-run 옵션으로 실제 변경 없이 계획만 출력.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from curhouse.aws.s3_manifests import get_manifest_json, list_manifests
from curhouse.aws.session import get_session
from curhouse.clickhouse.client import get_client, list_table_columns
from curhouse.clickhouse.schema import manifest_to_ddl
from curhouse.config import load_config
from curhouse.state import load_state, save_state, State

BACKUP_TABLE_SUFFIX = "_v1_backup"


def _has_source_column(client, database: str, table: str) -> bool:
    cols = list_table_columns(client, database, table)
    return any(c["name"] == "_source" for c in cols)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    client = get_client(cfg.clickhouse)
    db = cfg.clickhouse.database
    tbl = cfg.clickhouse.table
    backup = tbl + BACKUP_TABLE_SUFFIX

    existing_cols = list_table_columns(client, db, tbl)
    if not existing_cols:
        print(f"Table {db}.{tbl} does not exist — nothing to migrate.")
        return 0
    if _has_source_column(client, db, tbl):
        print(f"Table {db}.{tbl} already has _source column — already on v2.")
        return 0

    # 백업 테이블 이미 있으면 이전 실패한 마이그레이션 자국 — 안전을 위해 중단
    if list_table_columns(client, db, backup):
        print(
            f"error: {db}.{backup} already exists — "
            "previous migration attempt left artifacts. Clean up first.",
            file=sys.stderr,
        )
        return 1

    # 새 스키마 DDL 을 만들기 위해 최신 매니페스트 fetch
    session = get_session(cfg)
    s3 = session.client("s3", region_name=cfg.aws.region)
    dev_source = next((s for s in cfg.sources if s.name == "dev"), cfg.sources[0])
    manifests = list_manifests(
        s3,
        bucket=cfg.cur.bucket_name,
        prefix=dev_source.prefix,
        export_name=dev_source.export_name,
    )
    if not manifests:
        print(f"error: no manifests in S3 for {dev_source.name}", file=sys.stderr)
        return 1
    latest = max(manifests, key=lambda m: m.billing_period)
    manifest_json = get_manifest_json(s3, cfg.cur.bucket_name, latest.key)
    new_ddl = manifest_to_ddl(manifest_json, db, tbl)

    rename_sql = f"RENAME TABLE {db}.{tbl} TO {db}.{backup}"

    print("=== Migration plan ===")
    print(f"1) {rename_sql}")
    print(f"2) CREATE TABLE {db}.{tbl}  (new v2 schema with (_source, _billing_period) PARTITION BY)")
    print(f"3) Clear 'dev' source manifests from {cfg.state.path} → next sync re-loads everything")
    print(f"4) Run: curhouse sync  (re-loads dev + first-loads qa/prod)")
    print()
    print(f"After verification, DROP TABLE {db}.{backup} to free space.")
    print()

    if args.dry_run:
        print("--dry-run set — no changes applied.")
        return 0

    print(f"Renaming {db}.{tbl} → {db}.{backup} ...")
    client.command(rename_sql)

    print(f"Creating new {db}.{tbl} with v2 schema ...")
    client.command(new_ddl)

    # state 에서 dev 소스 매니페스트를 비워 재sync 를 강제
    old_state = load_state(cfg.state.path)
    new_sources = {k: v for k, v in old_state.sources.items() if k != "dev"}
    new_state = State(
        schema_version=old_state.schema_version,
        table_created_at=None,   # 새 테이블은 curhouse sync 가 다시 기록
        ddl_hash=None,
        sources=new_sources,
    )
    save_state(cfg.state.path, new_state)
    print(f"Cleared dev manifests from {cfg.state.path}")

    print()
    print("✓ Migration prep done. Next steps:")
    print("  1) curhouse sync         # dev + qa + prod 다 로드")
    print("  2) Metabase 대시보드 확인 (계정 필터에 3개 계정 다 뜨는지)")
    print(f"  3) 문제 없으면: DROP TABLE {db}.{backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
