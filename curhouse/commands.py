from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys

from curhouse.aws.cur_setup import (
    ensure_bucket_policy,
    ensure_cur_export,
    ensure_s3_bucket,
)
from curhouse.aws.s3_manifests import (
    ManifestInfo,
    get_manifest_json,
    list_manifests,
)
from curhouse.aws.session import get_session
from curhouse.clickhouse.client import (
    ensure_database,
    get_client,
    list_table_columns,
)
from curhouse.clickhouse.loader import build_s3_url, reload_partition
from curhouse.clickhouse.schema import ddl_hash, manifest_to_ddl
from curhouse.config import Config, SourceConfig
from curhouse.state import ManifestRecord, State, load_state, save_state

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _resolve_source(cfg: Config, name: str | None) -> SourceConfig:
    """cmd_setup / cmd_init_schema 처럼 단일 소스만 다루는 커맨드용 헬퍼."""
    if name is None:
        if len(cfg.sources) > 1:
            names = ", ".join(s.name for s in cfg.sources)
            raise SystemExit(
                f"multiple sources defined ({names}); "
                "specify one with --source NAME"
            )
        return cfg.sources[0]
    for s in cfg.sources:
        if s.name == name:
            return s
    available = ", ".join(s.name for s in cfg.sources)
    raise SystemExit(f"no source named {name!r} (available: {available})")


def cmd_setup(cfg: Config, args: argparse.Namespace) -> int:
    source = _resolve_source(cfg, getattr(args, "source", None))
    session = get_session(cfg)
    s3 = session.client("s3", region_name=cfg.aws.region)
    exports = session.client("bcm-data-exports", region_name=cfg.aws.region)

    ensure_s3_bucket(s3, bucket=cfg.cur.bucket_name, region=cfg.aws.region)
    ensure_bucket_policy(
        s3,
        bucket=cfg.cur.bucket_name,
        account_id=cfg.aws.account_id,
        region=cfg.aws.region,
    )
    arn = ensure_cur_export(
        exports,
        export_name=source.export_name,
        bucket=cfg.cur.bucket_name,
        prefix=source.prefix,
        region=cfg.aws.region,
        time_granularity=cfg.cur.time_granularity,
        include_resources=cfg.cur.include_resources,
        include_split_cost_allocation=cfg.cur.include_split_cost_allocation,
    )

    print(
        f"✓ S3 bucket: {cfg.cur.bucket_name}\n"
        f"✓ Bucket policy: applied\n"
        f"✓ CUR export [{source.name}]: {source.export_name} ({arn})\n"
        "ℹ Initial data delivery may take up to 24 hours.\n"
        f"ℹ Run `curhouse sync` after data appears in "
        f"s3://{cfg.cur.bucket_name}/{source.prefix}/{source.export_name}/data/"
    )
    return 0


def _diff_manifests(
    manifests: list[ManifestInfo], known: dict[str, ManifestRecord]
) -> list[ManifestInfo]:
    changed: list[ManifestInfo] = []
    for m in manifests:
        rec = known.get(m.billing_period)
        if rec is None or rec.etag != m.etag:
            changed.append(m)
    return changed


def _ensure_table(
    client, cfg: Config, manifest_json: dict, state: State
) -> tuple[State, bool]:
    """테이블 생성 (없으면). state 업데이트본 반환. 새로 만들어졌으면 True."""
    ensure_database(client, cfg.clickhouse.database)
    cols = list_table_columns(client, cfg.clickhouse.database, cfg.clickhouse.table)
    if cols:
        return state, False

    ddl = manifest_to_ddl(manifest_json, cfg.clickhouse.database, cfg.clickhouse.table)
    logger.info("Creating table %s.%s", cfg.clickhouse.database, cfg.clickhouse.table)
    logger.debug("DDL:\n%s", ddl)
    client.command(ddl)
    new_state = State(
        schema_version=state.schema_version,
        table_created_at=_now_iso(),
        ddl_hash=ddl_hash(ddl),
        sources=state.sources,
    )
    return new_state, True


def _record_synced(
    state: State, source_name: str, period: str, rec: ManifestRecord
) -> State:
    source_manifests = {**state.manifests_for(source_name), period: rec}
    new_sources = {**state.sources, source_name: source_manifests}
    return State(
        schema_version=state.schema_version,
        table_created_at=state.table_created_at,
        ddl_hash=state.ddl_hash,
        sources=new_sources,
    )


def cmd_sync(cfg: Config, args: argparse.Namespace) -> int:
    only_source = getattr(args, "only_source", None)
    only_period = getattr(args, "only_period", None)

    if only_source:
        sources_to_sync = tuple(s for s in cfg.sources if s.name == only_source)
        if not sources_to_sync:
            available = ", ".join(s.name for s in cfg.sources)
            print(
                f"No source named {only_source!r} (available: {available})",
                file=sys.stderr,
            )
            return 1
    else:
        sources_to_sync = cfg.sources

    session = get_session(cfg)
    s3 = session.client("s3", region_name=cfg.aws.region)
    creds = session.get_credentials().get_frozen_credentials()

    state = load_state(cfg.state.path)
    client = None  # 실제 로드가 필요할 때만 CH 접속
    failures = 0
    synced = 0

    for source in sources_to_sync:
        manifests = list_manifests(
            s3,
            bucket=cfg.cur.bucket_name,
            prefix=source.prefix,
            export_name=source.export_name,
        )
        if not manifests:
            print(f"[{source.name}] no manifests yet in S3 — skipping")
            continue

        known = state.manifests_for(source.name)
        if only_period:
            changed = [m for m in manifests if m.billing_period == only_period]
            if not changed:
                print(f"[{source.name}] no manifest for {only_period}")
                continue
        else:
            changed = _diff_manifests(manifests, known)

        if not changed:
            print(f"[{source.name}] up to date")
            continue

        if client is None:
            client = get_client(cfg.clickhouse)
            first_json = get_manifest_json(s3, cfg.cur.bucket_name, changed[0].key)
            state, created = _ensure_table(client, cfg, first_json, state)
            if created:
                save_state(cfg.state.path, state)

        for m in changed:
            url = build_s3_url(
                bucket=cfg.cur.bucket_name,
                region=cfg.aws.region,
                prefix=source.prefix,
                export_name=source.export_name,
                billing_period=m.billing_period,
            )
            try:
                row_count = reload_partition(
                    client,
                    database=cfg.clickhouse.database,
                    table=cfg.clickhouse.table,
                    source=source.name,
                    billing_period=m.billing_period,
                    s3_url=url,
                    access_key=creds.access_key,
                    secret_key=creds.secret_key,
                )
            except Exception as e:
                logger.exception(
                    "[%s] Failed to load billing period %s: %s",
                    source.name, m.billing_period, e,
                )
                failures += 1
                continue

            state = _record_synced(
                state,
                source.name,
                m.billing_period,
                ManifestRecord(
                    etag=m.etag,
                    last_modified=m.last_modified,
                    last_synced_at=_now_iso(),
                    row_count=row_count,
                ),
            )
            save_state(cfg.state.path, state)
            print(f"✓ [{source.name}] {m.billing_period}: {row_count:,} rows")
            synced += 1

    if failures:
        print(f"Completed with {failures} failures.", file=sys.stderr)
        return 1
    if synced == 0:
        print("Nothing to do — all sources/periods are up to date.")
    else:
        print(f"Sync complete ({synced} partition(s)).")
    return 0


def cmd_status(cfg: Config, _args: argparse.Namespace) -> int:
    state = load_state(cfg.state.path)
    if not state.sources:
        print("No state yet — run `curhouse sync` first.")
        return 0

    print(f"Table created: {state.table_created_at or '(unknown)'}")
    print(f"DDL hash: {state.ddl_hash or '(unknown)'}")

    grand_total = 0
    grand_periods = 0
    for source_name in sorted(state.sources):
        periods = state.sources[source_name]
        source_total = sum(rec.row_count for rec in periods.values())
        grand_total += source_total
        grand_periods += len(periods)
        print(f"\n[{source_name}]")
        for period in sorted(periods):
            rec = periods[period]
            print(
                f"  {period}: {rec.row_count:>12,} rows  "
                f"(synced {rec.last_synced_at})"
            )
        print(
            f"  subtotal: {source_total:,} rows across {len(periods)} periods"
        )
    print(
        f"\nTotal: {grand_total:,} rows across {grand_periods} "
        f"(source, period) partitions"
    )
    return 0


def cmd_init_schema(cfg: Config, args: argparse.Namespace) -> int:
    source = _resolve_source(cfg, getattr(args, "source", None))
    session = get_session(cfg)
    s3 = session.client("s3", region_name=cfg.aws.region)

    manifests = list_manifests(
        s3,
        bucket=cfg.cur.bucket_name,
        prefix=source.prefix,
        export_name=source.export_name,
    )
    if not manifests:
        print(
            f"No manifests found yet for source {source.name!r}.",
            file=sys.stderr,
        )
        return 1

    latest = max(manifests, key=lambda m: m.billing_period)
    manifest_json = get_manifest_json(s3, cfg.cur.bucket_name, latest.key)

    client = get_client(cfg.clickhouse)
    ensure_database(client, cfg.clickhouse.database)
    cols = list_table_columns(client, cfg.clickhouse.database, cfg.clickhouse.table)
    if cols:
        print(
            f"Table {cfg.clickhouse.database}.{cfg.clickhouse.table} already exists. "
            "Drop it first if you want to recreate."
        )
        return 0

    ddl = manifest_to_ddl(manifest_json, cfg.clickhouse.database, cfg.clickhouse.table)
    client.command(ddl)

    state = load_state(cfg.state.path)
    state = State(
        schema_version=state.schema_version,
        table_created_at=_now_iso(),
        ddl_hash=ddl_hash(ddl),
        sources=state.sources,
    )
    save_state(cfg.state.path, state)
    print(f"✓ Created {cfg.clickhouse.database}.{cfg.clickhouse.table}")
    return 0
