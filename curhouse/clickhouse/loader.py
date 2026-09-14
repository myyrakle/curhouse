from __future__ import annotations

from clickhouse_connect.driver.client import Client


# _source / _billing_period / _ingested_at 는 INSERT 시 리터럴로 채우므로
# parquet 에서 읽지 않고 타겟 컬럼 목록에서도 제외한다.
_INTERNAL_COLUMNS = ("_source", "_billing_period", "_ingested_at")


def build_s3_url(
    *, bucket: str, region: str, prefix: str, export_name: str, billing_period: str
) -> str:
    return (
        f"https://{bucket}.s3.{region}.amazonaws.com/"
        f"{prefix}/{export_name}/data/BILLING_PERIOD={billing_period}/*.parquet"
    )


def get_cur_columns(
    client: Client, database: str, table: str
) -> list[tuple[str, str]]:
    """CUR 데이터 컬럼 목록 (_source/_billing_period/_ingested_at 제외).

    parquet 과 이름으로 매칭할 때 필요한 (name, type) 튜플.
    system.columns 의 position 순으로 반환 → 이 순서가 INSERT 컬럼 리스트와
    SELECT 리스트의 순서가 된다 (정렬은 같은 데이터라도 안정성 확보).
    """
    result = client.query(
        "SELECT name, type FROM system.columns "
        "WHERE database = {db:String} AND table = {tbl:String} "
        "AND name NOT IN ('_source', '_billing_period', '_ingested_at') "
        "ORDER BY position",
        parameters={"db": database, "tbl": table},
    )
    return [(str(row[0]), str(row[1])) for row in result.result_rows]


def _quote_ident(name: str) -> str:
    # ClickHouse identifier — 백틱 안에 백틱은 이스케이프. CUR 컬럼명에 백틱은
    # 실무상 나올 수 없지만 방어적으로 처리.
    return "`" + name.replace("`", "``") + "`"


def build_s3_structure(cur_columns: list[tuple[str, str]]) -> str:
    # s3() 함수의 structure 파라미터 — 이 안의 컬럼명·타입이 parquet 읽기의
    # 진실의 출처. 이름으로 매칭하고 없는 컬럼은 setting 으로 NULL/기본값 처리.
    return ", ".join(f"{_quote_ident(n)} {t}" for n, t in cur_columns)


def build_insert_from_s3_sql(
    *,
    database: str,
    table: str,
    source: str,
    billing_period: str,
    cur_columns: list[tuple[str, str]],
) -> str:
    """INSERT INTO t(a,b,c, _source,_billing_period,_ingested_at)
    SELECT a,b,c, 'src','period', now64(3) FROM s3(..., 'Parquet', 'a T1, b T2, ...').

    핵심 세 가지:
      1) 명시적 컬럼 리스트 — INSERT 도 SELECT 도 이름으로 매칭.
         `SELECT *` 위치 매칭은 parquet 컬럼 수가 target 과 다르면 조용히
         엉뚱한 자리에 값을 붙여넣어 타입 미스매치 → 다중 소스에서 반드시 재현됨
         (dev 매니페스트로 만든 테이블 vs qa/prod parquet 이 열 수 다름).
      2) s3() 에 structure 명시 — parquet 스키마 추론에 맡기지 않고 우리가
         선언한 컬럼 이름·타입으로 강제 읽기.
      3) allow_missing_columns + null_as_default —
         소스 parquet 에 없는 컬럼은 자동으로 NULL/기본값 → 소스간 스키마 편차
         (dev 는 cost_category 있지만 qa 는 없음 같은 케이스) 흡수.
    """
    col_idents = [_quote_ident(n) for n, _ in cur_columns]
    insert_cols = ",\n  ".join(col_idents + [
        _quote_ident("_source"),
        _quote_ident("_billing_period"),
        _quote_ident("_ingested_at"),
    ])
    select_cols = ",\n  ".join(col_idents + [
        f"'{source}' AS _source",
        f"'{billing_period}' AS _billing_period",
        "now64(3) AS _ingested_at",
    ])
    structure = build_s3_structure(cur_columns)
    return (
        f"INSERT INTO {database}.{table} (\n  {insert_cols}\n)\n"
        f"SELECT\n  {select_cols}\n"
        "FROM s3(\n"
        "  {url:String},\n"
        "  {access_key:String},\n"
        "  {secret_key:String},\n"
        "  'Parquet',\n"
        f"  {_sql_string_literal(structure)}\n"
        ")\n"
        "SETTINGS input_format_parquet_allow_missing_columns = 1, "
        "input_format_null_as_default = 1"
    )


def _sql_string_literal(s: str) -> str:
    # structure 는 사용자 입력이 아니라 system.columns 결과에서 온 거지만
    # 문자열 리터럴 안에 작은따옴표가 나오면 sql 이 깨지므로 표준 방식으로 이스케이프.
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def staging_table_name(table: str, source: str, billing_period: str) -> str:
    # ClickHouse identifier에 하이픈 불가 → 2026-08 → 2026_08
    # 소스명 포함해서 dev/qa/prod 동시 sync 시에도 스테이징 충돌 없음
    return f"{table}__stage_{source}_{billing_period.replace('-', '_')}"


def build_drop_stage_table_sql(database: str, stage_table: str) -> str:
    return f"DROP TABLE IF EXISTS {database}.{stage_table}"


def build_create_stage_table_sql(
    database: str, stage_table: str, source_table: str
) -> str:
    # CREATE ... AS 는 ENGINE/PARTITION BY/ORDER BY까지 복제 → REPLACE PARTITION 호환
    return f"CREATE TABLE {database}.{stage_table} AS {database}.{source_table}"


def build_replace_partition_sql(
    database: str, table: str, stage_table: str, source: str, billing_period: str
) -> str:
    # 튜플 파티션 (_source, _billing_period) 지정
    return (
        f"ALTER TABLE {database}.{table} "
        f"REPLACE PARTITION tuple('{source}', '{billing_period}') "
        f"FROM {database}.{stage_table}"
    )


def reload_partition(
    client: Client,
    *,
    database: str,
    table: str,
    source: str,
    billing_period: str,
    s3_url: str,
    access_key: str,
    secret_key: str,
) -> int:
    """(source, billing_period) 파티션을 원자적으로 교체하고 row 수 반환.

    이전 구현은 DROP PARTITION → INSERT FROM s3() 순차였고,
    s3() glob이 0개 파일을 매칭하면 예외 없이 0행이 들어와 파티션이 영구 유실됐다
    (AWS CUR이 월별 parquet를 재생성하는 순간 cron이 걸리면 재발).
    스테이징 테이블에 먼저 적재 → 0행이면 예외 → 원본 그대로 두고 종료.
    비-0행이면 REPLACE PARTITION FROM 으로 원자 교체.

    파티션 키가 (_source, _billing_period) 튜플이므로 dev/qa/prod가
    같은 billing_period를 가져도 서로 다른 파티션 → 완전 격리.
    """
    stage = staging_table_name(table, source, billing_period)
    client.command(build_drop_stage_table_sql(database, stage))
    client.command(build_create_stage_table_sql(database, stage, table))
    try:
        # 매번 타겟 테이블 컬럼을 조회 — 스키마가 진화해도 다음 sync 가 알아서
        # 새 컬럼 리스트를 물고 INSERT 하도록 (하드코딩된 컬럼 목록 유지비 절감).
        cur_columns = get_cur_columns(client, database, stage)
        client.command(
            build_insert_from_s3_sql(
                database=database,
                table=stage,
                source=source,
                billing_period=billing_period,
                cur_columns=cur_columns,
            ),
            parameters={
                "url": s3_url,
                "access_key": access_key,
                "secret_key": secret_key,
            },
        )
        count_sql = (
            f"SELECT count() FROM {database}.{stage} "
            "WHERE _source = {src:String} AND _billing_period = {bp:String}"
        )
        result = client.query(
            count_sql, parameters={"src": source, "bp": billing_period}
        )
        row_count = int(result.result_rows[0][0])
        if row_count == 0:
            raise RuntimeError(
                f"S3 returned 0 rows for {source}/{billing_period}; "
                "refusing to replace main partition (would wipe it)."
            )
        client.command(
            build_replace_partition_sql(
                database, table, stage, source, billing_period
            )
        )
        return row_count
    finally:
        client.command(build_drop_stage_table_sql(database, stage))
