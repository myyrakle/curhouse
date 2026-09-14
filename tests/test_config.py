from pathlib import Path

import pytest

from curhouse.config import Config, load_config

_SAMPLE_CONFIG = """
[aws]
profile = "test-profile"
region = "us-east-1"
account_id = "111111111111"

[cur]
bucket_name = "test-bucket"
time_granularity = "HOURLY"
include_resources = true
include_split_cost_allocation = true

[[sources]]
name = "dev"
prefix = "cur2"
export_name = "datahouse-cur-export"

[[sources]]
name = "qa"
prefix = "qa"
export_name = "cur_to_dev"

[[sources]]
name = "prod"
prefix = "prod"
export_name = "cur_to_dev"

[clickhouse]
host = "ch.local"
port = 8123
user = "default"
password = "filepw"
database = "aws_billing"
table = "cur_line_items"
secure = false

[state]
path = ".state.json"
"""


@pytest.fixture
def sample_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(_SAMPLE_CONFIG)
    return p


def test_load_config_basic(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    assert isinstance(cfg, Config)
    assert cfg.aws.profile == "test-profile"
    assert cfg.cur.bucket_name == "test-bucket"
    assert cfg.cur.time_granularity == "HOURLY"
    assert cfg.clickhouse.host == "ch.local"
    assert cfg.clickhouse.password == "filepw"
    assert cfg.state.path == ".state.json"


def test_load_config_parses_all_sources(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    names = [s.name for s in cfg.sources]
    assert names == ["dev", "qa", "prod"]
    assert cfg.sources[0].prefix == "cur2"
    assert cfg.sources[0].export_name == "datahouse-cur-export"
    assert cfg.sources[1].prefix == "qa"
    assert cfg.sources[2].export_name == "cur_to_dev"


def test_env_password_overrides_file(
    sample_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CURHOUSE_CH_PASSWORD", "envpw")
    cfg = load_config(sample_config_path)
    assert cfg.clickhouse.password == "envpw"


def test_invalid_granularity_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        _SAMPLE_CONFIG.replace('time_granularity = "HOURLY"',
                               'time_granularity = "WEEKLY"')
    )
    with pytest.raises(ValueError, match="time_granularity"):
        load_config(p)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_missing_profile_defaults_empty(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(_SAMPLE_CONFIG.replace('profile = "test-profile"\n', ""))
    cfg = load_config(p)
    assert cfg.aws.profile == ""


def test_missing_sources_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    # [[sources]] 블록 세 개를 통째로 잘라내면 sources 배열이 사라짐
    stripped = _SAMPLE_CONFIG.split("[[sources]]")[0] + _SAMPLE_CONFIG.split("[clickhouse]")[1].join(["[clickhouse]", ""])
    p.write_text(_SAMPLE_CONFIG.split("[[sources]]")[0] + "\n[clickhouse]" + _SAMPLE_CONFIG.split("[clickhouse]")[1])
    with pytest.raises(ValueError, match="at least one \\[\\[sources\\]\\]"):
        load_config(p)


def test_duplicate_source_name_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(_SAMPLE_CONFIG.replace('name = "qa"', 'name = "dev"'))
    with pytest.raises(ValueError, match="duplicate source name"):
        load_config(p)


def test_invalid_source_name_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(_SAMPLE_CONFIG.replace('name = "qa"', 'name = "Q-A"'))
    with pytest.raises(ValueError, match="invalid source name"):
        load_config(p)


def test_source_missing_field_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(_SAMPLE_CONFIG.replace(
        'name = "qa"\nprefix = "qa"\nexport_name = "cur_to_dev"',
        'name = "qa"\nexport_name = "cur_to_dev"',
    ))
    with pytest.raises(ValueError, match="missing required field: prefix"):
        load_config(p)


def test_env_ch_host_port_override(
    sample_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CURHOUSE_CH_HOST", "clickhouse")
    monkeypatch.setenv("CURHOUSE_CH_PORT", "9000")
    cfg = load_config(sample_config_path)
    assert cfg.clickhouse.host == "clickhouse"
    assert cfg.clickhouse.port == 9000


def test_env_state_path_override(
    sample_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CURHOUSE_STATE_PATH", "/state/state.json")
    cfg = load_config(sample_config_path)
    assert cfg.state.path == "/state/state.json"


def test_env_ch_user_override(
    sample_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CURHOUSE_CH_USER", "readonly")
    cfg = load_config(sample_config_path)
    assert cfg.clickhouse.user == "readonly"


def test_env_aws_and_cur_overrides(
    sample_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 회사·계정 식별 정보는 파일에 박지 않고 env 로 주입 가능해야 함
    monkeypatch.setenv("CURHOUSE_AWS_PROFILE", "prod-profile")
    monkeypatch.setenv("CURHOUSE_AWS_REGION", "ap-northeast-2")
    monkeypatch.setenv("CURHOUSE_AWS_ACCOUNT_ID", "987654321012")
    monkeypatch.setenv("CURHOUSE_CUR_BUCKET_NAME", "actual-bucket")
    cfg = load_config(sample_config_path)
    assert cfg.aws.profile == "prod-profile"
    assert cfg.aws.region == "ap-northeast-2"
    assert cfg.aws.account_id == "987654321012"
    assert cfg.cur.bucket_name == "actual-bucket"
