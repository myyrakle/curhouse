import argparse
from unittest.mock import MagicMock

from curhouse.commands import _diff_manifests, cmd_status
from curhouse.config import (
    AwsConfig,
    ClickhouseConfig,
    Config,
    CurConfig,
    SourceConfig,
    StateConfig,
)
from curhouse.state import ManifestRecord, State, save_state


def _make_cfg(tmp_path) -> Config:
    return Config(
        aws=AwsConfig(profile="p", region="us-east-1", account_id="111"),
        cur=CurConfig(
            bucket_name="b",
            time_granularity="HOURLY",
            include_resources=True,
            include_split_cost_allocation=True,
        ),
        sources=(
            SourceConfig(name="dev", prefix="cur2", export_name="dev-export"),
            SourceConfig(name="qa", prefix="qa", export_name="cur_to_dev"),
        ),
        clickhouse=ClickhouseConfig(
            host="h", port=8123, user="u", password="",
            database="d", table="t", secure=False,
        ),
        state=StateConfig(path=str(tmp_path / "state.json")),
    )


def test_diff_manifests_new_period() -> None:
    known: dict[str, ManifestRecord] = {}
    manifests = [
        MagicMock(billing_period="2026-04", etag='"abc"', last_modified="t1"),
    ]
    changed = _diff_manifests(manifests, known)
    assert [m.billing_period for m in changed] == ["2026-04"]


def test_diff_manifests_unchanged_skipped() -> None:
    known = {
        "2026-04": ManifestRecord(
            etag='"abc"', last_modified="t1", last_synced_at="t1", row_count=10,
        )
    }
    manifests = [
        MagicMock(billing_period="2026-04", etag='"abc"', last_modified="t1"),
    ]
    assert _diff_manifests(manifests, known) == []


def test_diff_manifests_etag_changed_returned() -> None:
    known = {
        "2026-04": ManifestRecord(
            etag='"OLD"', last_modified="t1", last_synced_at="t1", row_count=10,
        )
    }
    manifests = [
        MagicMock(billing_period="2026-04", etag='"NEW"', last_modified="t2"),
    ]
    changed = _diff_manifests(manifests, known)
    assert [m.billing_period for m in changed] == ["2026-04"]


def test_diff_manifests_is_per_source(tmp_path) -> None:
    # dev의 2026-08 이 이미 있다는 사실은 qa의 diff에 영향 주면 안 됨
    dev_known = {
        "2026-08": ManifestRecord(
            etag='"dev-etag"', last_modified="t", last_synced_at="t", row_count=1
        )
    }
    qa_known: dict[str, ManifestRecord] = {}   # qa는 처음
    manifests = [
        MagicMock(billing_period="2026-08", etag='"dev-etag"', last_modified="t"),
    ]
    assert _diff_manifests(manifests, dev_known) == []   # dev: 이미 있음
    assert len(_diff_manifests(manifests, qa_known)) == 1   # qa: 첫 로드 필요


def test_cmd_status_prints_empty_state(tmp_path, capsys) -> None:
    cfg = _make_cfg(tmp_path)
    rc = cmd_status(cfg, argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "No state yet" in out


def test_cmd_status_prints_per_source_breakdown(tmp_path, capsys) -> None:
    cfg = _make_cfg(tmp_path)
    state = State(
        table_created_at="2026-09-10T00:00:00Z",
        ddl_hash="sha256:abc",
        sources={
            "dev": {
                "2026-08": ManifestRecord(
                    etag='"e"', last_modified="t", last_synced_at="s",
                    row_count=1_000_000,
                ),
            },
            "qa": {
                "2026-09": ManifestRecord(
                    etag='"e"', last_modified="t", last_synced_at="s",
                    row_count=50_000,
                ),
            },
        },
    )
    save_state(cfg.state.path, state)
    rc = cmd_status(cfg, argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dev]" in out
    assert "[qa]" in out
    assert "1,000,000" in out
    assert "50,000" in out
    assert "Total: 1,050,000" in out
