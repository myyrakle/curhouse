from unittest.mock import MagicMock

import pytest

from curhouse.clickhouse.loader import (
    build_create_stage_table_sql,
    build_drop_stage_table_sql,
    build_insert_from_s3_sql,
    build_replace_partition_sql,
    build_s3_structure,
    build_s3_url,
    reload_partition,
    staging_table_name,
)


_SAMPLE_CUR_COLUMNS = [
    ("line_item_usage_start_date", "DateTime64(3, 'UTC')"),
    ("line_item_product_code", "LowCardinality(String)"),
    ("line_item_unblended_cost", "Nullable(Float64)"),
    ("resource_tags", "Map(String, String)"),
]


def test_build_s3_url_uses_virtual_hosted_style() -> None:
    url = build_s3_url(
        bucket="my-bucket",
        region="us-east-1",
        prefix="cur2",
        export_name="exp",
        billing_period="2026-04",
    )
    assert url == (
        "https://my-bucket.s3.us-east-1.amazonaws.com/"
        "cur2/exp/data/BILLING_PERIOD=2026-04/*.parquet"
    )


def test_build_s3_structure_quotes_identifiers() -> None:
    s = build_s3_structure(_SAMPLE_CUR_COLUMNS)
    assert "`line_item_usage_start_date` DateTime64(3, 'UTC')" in s
    assert "`resource_tags` Map(String, String)" in s
    # 컬럼 4개 → 사이 구분자 ", " 3개 (타입 내부의 콤마와 구분해서 세야 함)
    assert s.count(", `") == 3


def test_build_insert_sql_uses_named_columns_and_structure() -> None:
    sql = build_insert_from_s3_sql(
        database="aws_billing",
        table="cur_line_items",
        source="qa",
        billing_period="2026-04",
        cur_columns=_SAMPLE_CUR_COLUMNS,
    )
    # 명시적 컬럼 리스트 — 이름 기반 매칭 (위치 매칭이면 다중 소스 스키마 편차 시 실패)
    assert "INSERT INTO aws_billing.cur_line_items (" in sql
    assert "`line_item_usage_start_date`" in sql
    assert "`_source`" in sql and "`_billing_period`" in sql and "`_ingested_at`" in sql
    # SELECT 도 같은 순서·이름
    assert "SELECT" in sql
    assert "'qa' AS _source" in sql
    assert "'2026-04' AS _billing_period" in sql
    assert "now64(3) AS _ingested_at" in sql
    # s3() structure 파라미터 — parquet 읽기의 진실의 출처
    assert "'Parquet'" in sql
    # structure 는 SQL 문자열 리터럴 안에 들어가므로 내부 작은따옴표는 백슬래시 이스케이프
    assert "line_item_usage_start_date` DateTime64(3, \\'UTC\\')" in sql
    # 소스간 스키마 편차 흡수 세팅
    assert "input_format_parquet_allow_missing_columns = 1" in sql
    assert "input_format_null_as_default = 1" in sql
    assert "{url:String}" in sql
    assert "{access_key:String}" in sql
    assert "{secret_key:String}" in sql


def test_build_insert_sql_escapes_single_quotes_in_structure() -> None:
    # DateTime64(3, 'UTC') 같은 타입 문자열엔 작은따옴표가 들어감 → 이스케이프 필요
    sql = build_insert_from_s3_sql(
        database="d", table="t", source="dev", billing_period="2026-01",
        cur_columns=[("ts", "DateTime64(3, 'UTC')")],
    )
    # structure 문자열 리터럴 안의 따옴표는 백슬래시 이스케이프
    assert "DateTime64(3, \\'UTC\\')" in sql


def test_staging_table_name_includes_source() -> None:
    # dev/qa/prod 동시 sync 시 스테이징 테이블 충돌 방지
    # ClickHouse는 identifier에 하이픈 불가 → 2026-08 → 2026_08
    assert staging_table_name("cur_line_items", "dev", "2026-08") == (
        "cur_line_items__stage_dev_2026_08"
    )
    assert staging_table_name("cur_line_items", "qa", "2026-08") == (
        "cur_line_items__stage_qa_2026_08"
    )


def test_build_drop_stage_uses_if_exists() -> None:
    # 이전 실행이 중단돼 남은 스테이징이 있어도 새 실행이 실패하지 않게
    assert build_drop_stage_table_sql("d", "t__stage_dev_2026_08") == (
        "DROP TABLE IF EXISTS d.t__stage_dev_2026_08"
    )


def test_build_create_stage_clones_source() -> None:
    # AS <main> 은 ENGINE/PARTITION BY/ORDER BY까지 복제 →
    # REPLACE PARTITION FROM 이 요구하는 스키마 동등성 만족
    sql = build_create_stage_table_sql("d", "t__stage_dev_2026_08", "t")
    assert sql == "CREATE TABLE d.t__stage_dev_2026_08 AS d.t"


def test_build_replace_partition_uses_tuple_form() -> None:
    # 파티션 키가 (_source, _billing_period) 튜플이므로 REPLACE도 튜플 지정 필수
    sql = build_replace_partition_sql(
        "d", "t", "t__stage_dev_2026_08", "dev", "2026-08"
    )
    assert sql == (
        "ALTER TABLE d.t REPLACE PARTITION tuple('dev', '2026-08') "
        "FROM d.t__stage_dev_2026_08"
    )


def _fake_client(*, stage_row_count: int, cur_cols=_SAMPLE_CUR_COLUMNS) -> MagicMock:
    """query() 는 두 종류의 호출을 받는다:
       1) get_cur_columns — system.columns (name, type) rows
       2) count(*) — 스테이지 row 수
    호출 순서로 구분."""
    client = MagicMock()
    call_seq = {"n": 0}

    def _query(*args, **kwargs):
        call_seq["n"] += 1
        result = MagicMock()
        if call_seq["n"] == 1:
            result.result_rows = list(cur_cols)
        else:
            result.result_rows = [(stage_row_count,)]
        return result

    client.query.side_effect = _query
    return client


def test_reload_partition_replaces_when_stage_has_rows() -> None:
    client = _fake_client(stage_row_count=123)
    count = reload_partition(
        client,
        database="d",
        table="t",
        source="qa",
        billing_period="2026-08",
        s3_url="https://x/*.parquet",
        access_key="AK",
        secret_key="SK",
    )
    assert count == 123
    commands = [c.args[0] for c in client.command.call_args_list]
    # DROP IF EXISTS → CREATE stage → INSERT → REPLACE PARTITION → DROP stage
    assert commands[0] == "DROP TABLE IF EXISTS d.t__stage_qa_2026_08"
    assert commands[1] == "CREATE TABLE d.t__stage_qa_2026_08 AS d.t"
    assert "INSERT INTO d.t__stage_qa_2026_08" in commands[2]
    assert "'qa' AS _source" in commands[2]
    assert commands[3] == (
        "ALTER TABLE d.t REPLACE PARTITION tuple('qa', '2026-08') "
        "FROM d.t__stage_qa_2026_08"
    )
    assert commands[4] == "DROP TABLE IF EXISTS d.t__stage_qa_2026_08"


def test_reload_partition_raises_and_preserves_main_when_stage_empty() -> None:
    # 8/30·9/6 사고 회귀 테스트: s3()가 0행 반환해도 원본 파티션이
    # 통째로 날아가면 안 된다. 예외를 던지고, REPLACE PARTITION은 실행되지 않아야 한다.
    client = _fake_client(stage_row_count=0)
    with pytest.raises(RuntimeError, match="S3 returned 0 rows for prod/2026-09"):
        reload_partition(
            client,
            database="d",
            table="t",
            source="prod",
            billing_period="2026-09",
            s3_url="https://x/*.parquet",
            access_key="AK",
            secret_key="SK",
        )
    commands = [c.args[0] for c in client.command.call_args_list]
    # REPLACE PARTITION 절대 호출 안 됨 (원본 보존의 핵심)
    assert not any("REPLACE PARTITION" in c for c in commands), (
        f"REPLACE PARTITION must not run on empty stage; got: {commands}"
    )
    # 스테이징은 finally에서 정리
    assert commands[-1] == "DROP TABLE IF EXISTS d.t__stage_prod_2026_09"


def test_reload_partition_drops_stage_even_when_insert_fails() -> None:
    # INSERT가 예외를 던져도 스테이징 테이블은 finally로 정리돼야 한다
    client = _fake_client(stage_row_count=0)
    call_count = {"n": 0}

    def _command_side_effect(*args, **kwargs):
        call_count["n"] += 1
        # 3번째 command 호출 (INSERT) 에서 에러
        if call_count["n"] == 3:
            raise RuntimeError("simulated S3 auth failure")

    client.command.side_effect = _command_side_effect
    with pytest.raises(RuntimeError, match="simulated S3 auth failure"):
        reload_partition(
            client,
            database="d",
            table="t",
            source="dev",
            billing_period="2026-09",
            s3_url="https://x/*.parquet",
            access_key="AK",
            secret_key="SK",
        )
    commands = [c.args[0] for c in client.command.call_args_list]
    # 마지막은 반드시 스테이징 DROP
    assert commands[-1] == "DROP TABLE IF EXISTS d.t__stage_dev_2026_09"


def test_reload_partition_count_query_filters_by_both_source_and_period() -> None:
    # count는 스테이징 테이블 전체가 아니라 정확한 (source, period) 만 세야 한다
    client = _fake_client(stage_row_count=500)
    reload_partition(
        client,
        database="d",
        table="t",
        source="qa",
        billing_period="2026-09",
        s3_url="https://x/*.parquet",
        access_key="AK",
        secret_key="SK",
    )
    # query 호출 두 번째가 count SQL
    count_call = client.query.call_args_list[1]
    query_sql = count_call.args[0]
    assert "_source = {src:String}" in query_sql
    assert "_billing_period = {bp:String}" in query_sql
    assert count_call.kwargs["parameters"] == {"src": "qa", "bp": "2026-09"}
