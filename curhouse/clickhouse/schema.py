from __future__ import annotations

import hashlib

LOW_CARDINALITY_COLUMNS: frozenset[str] = frozenset({
    "product_servicecode",
    "product_region",
    "product_instance_type",
    "product_operation",
    "line_item_product_code",
    "line_item_usage_type",
    "line_item_line_item_type",
    "bill_billing_entity",
    "bill_payer_account_id",
    "line_item_usage_account_id",
})

_ORDER_BY_COLUMNS = (
    "line_item_usage_start_date",
    "line_item_product_code",
    "line_item_usage_account_id",
    "line_item_resource_id",
)


def map_cur_type(cur_type: str, column_name: str) -> str:
    """CUR 2.0 manifest 타입을 ClickHouse 타입으로 매핑.

    Actual CUR 2.0 manifest emits lowercase types: string, timestamp, double, map.
    AWS emits empty strings (not NULL) for missing string fields.
    """
    if cur_type == "string":
        if column_name in LOW_CARDINALITY_COLUMNS:
            return "LowCardinality(String)"
        return "String"
    if cur_type == "timestamp":
        # CUR 타임스탬프는 UTC 절대시각이다. TZ를 명시하지 않으면 toDate() 등이
        # 서버 로컬 TZ로 버킷팅해 AWS의 UTC 청구일과 어긋난다('Asia/Seoul'이면 +9h).
        # 명시적으로 'UTC'를 박아 조회/필터 타임존을 청구 기준과 일치시킨다.
        if column_name in _ORDER_BY_COLUMNS:
            return "DateTime64(3, 'UTC')"
        return "Nullable(DateTime64(3, 'UTC'))"
    if cur_type == "double":
        return "Nullable(Float64)"
    if cur_type == "map":
        return "Map(String, String)"
    raise ValueError(f"Unknown CUR type: {cur_type!r}")


def manifest_to_ddl(manifest: dict, database: str, table: str) -> str:
    columns = manifest.get("columns", [])
    if not columns:
        raise ValueError("manifest has no columns")

    column_lines: list[str] = []
    for col in columns:
        name = col["name"]
        ch_type = map_cur_type(col["type"], name)
        column_lines.append(f"  `{name}` {ch_type}")

    # 다중 소스(dev/qa/prod) 대응 — 파티션 첫 축이 _source 라서
    # 소스별 파티션이 완전히 격리되고, DROP/REPLACE PARTITION 이 다른 소스 데이터를 건드리지 않는다.
    column_lines.append("  `_source` LowCardinality(String)")
    column_lines.append("  `_billing_period` String")
    column_lines.append("  `_ingested_at` DateTime64(3) DEFAULT now64(3)")

    order_by = ", ".join(_ORDER_BY_COLUMNS)

    return (
        f"CREATE TABLE IF NOT EXISTS {database}.{table} (\n"
        + ",\n".join(column_lines)
        + "\n)\n"
        + "ENGINE = MergeTree\n"
        + "PARTITION BY (_source, _billing_period)\n"
        + f"ORDER BY ({order_by})\n"
        + "SETTINGS index_granularity = 8192"
    )


def ddl_hash(ddl: str) -> str:
    return "sha256:" + hashlib.sha256(ddl.encode("utf-8")).hexdigest()
