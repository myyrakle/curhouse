from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Granularity = Literal["HOURLY", "DAILY", "MONTHLY"]
ALLOWED_GRANULARITIES: tuple[Granularity, ...] = ("HOURLY", "DAILY", "MONTHLY")

# 소스명은 파티션 값·컬럼 리터럴로 그대로 들어가므로 안전한 문자만 허용
_SOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class AwsConfig:
    profile: str
    region: str
    account_id: str


@dataclass(frozen=True)
class CurConfig:
    """소스 공통 CUR export 설정. 소스별로 다른 것은 SourceConfig 로 분리."""

    bucket_name: str
    time_granularity: Granularity
    include_resources: bool
    include_split_cost_allocation: bool


@dataclass(frozen=True)
class SourceConfig:
    """CUR export 소스 (계정/환경) — 같은 버킷 안의 다른 경로."""

    name: str
    prefix: str
    export_name: str


@dataclass(frozen=True)
class ClickhouseConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    table: str
    secure: bool


@dataclass(frozen=True)
class StateConfig:
    path: str


@dataclass(frozen=True)
class Config:
    aws: AwsConfig
    cur: CurConfig
    sources: tuple[SourceConfig, ...]
    clickhouse: ClickhouseConfig
    state: StateConfig


def _parse_sources(raw_sources: list[dict]) -> tuple[SourceConfig, ...]:
    if not raw_sources:
        raise ValueError(
            "config must define at least one [[sources]] entry "
            "(name/prefix/export_name)"
        )
    seen: set[str] = set()
    parsed: list[SourceConfig] = []
    for s in raw_sources:
        name = s.get("name")
        if not name or not _SOURCE_NAME_RE.match(name):
            raise ValueError(
                f"invalid source name {name!r}: must match [a-z][a-z0-9_]*"
            )
        if name in seen:
            raise ValueError(f"duplicate source name: {name!r}")
        seen.add(name)
        try:
            parsed.append(
                SourceConfig(
                    name=name, prefix=s["prefix"], export_name=s["export_name"]
                )
            )
        except KeyError as e:
            raise ValueError(
                f"source {name!r} missing required field: {e.args[0]}"
            ) from None
    return tuple(parsed)


def load_config(path: Path | str) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")

    with path.open("rb") as f:
        data = tomllib.load(f)

    granularity = data["cur"]["time_granularity"]
    if granularity not in ALLOWED_GRANULARITIES:
        raise ValueError(
            f"time_granularity must be one of {ALLOWED_GRANULARITIES}, "
            f"got {granularity!r}"
        )

    ch_password = os.environ.get("CURHOUSE_CH_PASSWORD", data["clickhouse"]["password"])
    ch_host = os.environ.get("CURHOUSE_CH_HOST", data["clickhouse"]["host"])
    ch_port = int(os.environ.get("CURHOUSE_CH_PORT", data["clickhouse"]["port"]))
    ch_user = os.environ.get("CURHOUSE_CH_USER", data["clickhouse"]["user"])
    state_path = os.environ.get("CURHOUSE_STATE_PATH", data["state"]["path"])

    return Config(
        aws=AwsConfig(
            profile=data["aws"].get("profile", ""),
            region=data["aws"]["region"],
            account_id=str(data["aws"]["account_id"]),
        ),
        cur=CurConfig(
            bucket_name=data["cur"]["bucket_name"],
            time_granularity=granularity,
            include_resources=data["cur"]["include_resources"],
            include_split_cost_allocation=data["cur"][
                "include_split_cost_allocation"
            ],
        ),
        sources=_parse_sources(data.get("sources", [])),
        clickhouse=ClickhouseConfig(
            host=ch_host,
            port=ch_port,
            user=ch_user,
            password=ch_password,
            database=data["clickhouse"]["database"],
            table=data["clickhouse"]["table"],
            secure=bool(data["clickhouse"]["secure"]),
        ),
        state=StateConfig(path=state_path),
    )
