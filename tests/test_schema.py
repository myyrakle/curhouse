import pytest

from curhouse.clickhouse.schema import (
    LOW_CARDINALITY_COLUMNS,
    map_cur_type,
    manifest_to_ddl,
)


def test_map_cur_type_basic() -> None:
    assert (
        map_cur_type("timestamp", "bill_invoice_id")
        == "Nullable(DateTime64(3, 'UTC'))"
    )
    assert map_cur_type("double", "line_item_unblended_cost") == "Nullable(Float64)"
    assert map_cur_type("map", "resource_tags") == "Map(String, String)"


def test_map_cur_type_timestamp_in_order_by_is_non_nullable() -> None:
    assert (
        map_cur_type("timestamp", "line_item_usage_start_date")
        == "DateTime64(3, 'UTC')"
    )


def test_map_cur_type_string_low_cardinality() -> None:
    for col in LOW_CARDINALITY_COLUMNS:
        assert map_cur_type("string", col) == "LowCardinality(String)"


def test_map_cur_type_string_normal() -> None:
    assert map_cur_type("string", "line_item_resource_id") == "String"


def test_manifest_to_ddl_contains_partition_and_engine() -> None:
    manifest = {
        "columns": [
            {"name": "line_item_usage_start_date", "type": "timestamp"},
            {"name": "line_item_usage_account_id", "type": "string"},
            {"name": "line_item_product_code", "type": "string"},
            {"name": "line_item_resource_id", "type": "string"},
            {"name": "line_item_unblended_cost", "type": "double"},
            {"name": "resource_tags", "type": "map"},
        ]
    }
    ddl = manifest_to_ddl(manifest, "aws_billing", "cur_line_items")

    assert "CREATE TABLE IF NOT EXISTS aws_billing.cur_line_items" in ddl
    assert "`line_item_usage_start_date` DateTime64(3, 'UTC')" in ddl
    assert "`line_item_usage_account_id` LowCardinality(String)" in ddl
    assert "`line_item_product_code` LowCardinality(String)" in ddl
    assert "`line_item_resource_id` String" in ddl
    assert "`line_item_unblended_cost` Nullable(Float64)" in ddl
    assert "`resource_tags` Map(String, String)" in ddl
    assert "`_source` LowCardinality(String)" in ddl
    assert "`_billing_period` String" in ddl
    assert "`_ingested_at` DateTime64(3) DEFAULT now64(3)" in ddl
    assert "ENGINE = MergeTree" in ddl
    # 파티션 첫 축이 _source: 소스별 완전 격리 (DROP PARTITION 이 다른 소스를 안 건드림)
    assert "PARTITION BY (_source, _billing_period)" in ddl
    assert "ORDER BY" in ddl


def test_manifest_to_ddl_unknown_type_raises() -> None:
    manifest = {"columns": [{"name": "x", "type": "Spaceship"}]}
    with pytest.raises(ValueError, match="Spaceship"):
        manifest_to_ddl(manifest, "db", "t")
