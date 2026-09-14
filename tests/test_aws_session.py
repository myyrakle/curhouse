from __future__ import annotations

from pathlib import Path

import pytest

from curhouse.aws.session import get_session
from curhouse.config import (
    AwsConfig,
    ClickhouseConfig,
    Config,
    CurConfig,
    SourceConfig,
    StateConfig,
)


def _cfg(profile: str = "test-profile") -> Config:
    return Config(
        aws=AwsConfig(profile=profile, region="us-east-1", account_id="111"),
        cur=CurConfig(
            bucket_name="b",
            time_granularity="HOURLY",
            include_resources=True,
            include_split_cost_allocation=True,
        ),
        sources=(SourceConfig(name="dev", prefix="p", export_name="e"),),
        clickhouse=ClickhouseConfig(
            host="h", port=8123, user="u", password="",
            database="d", table="t", secure=False,
        ),
        state=StateConfig(path="s"),
    )


@pytest.fixture
def fake_aws_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create fake AWS credentials so boto3.Session does not raise ProfileNotFound."""
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "[default]\n"
        "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
        "\n"
        "[test-profile]\n"
        "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
    )
    config_file = tmp_path / "config"
    config_file.write_text("")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")


def test_get_session_uses_profile(fake_aws_credentials: None) -> None:
    session = get_session(_cfg(profile="default"))
    assert session.profile_name == "default"


def test_get_session_uses_region(fake_aws_credentials: None) -> None:
    session = get_session(_cfg())
    assert session.region_name == "us-east-1"


@pytest.fixture
def fake_aws_credentials_env_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create fake AWS credentials via env vars only (no [default] profile to force env fallback)."""
    credentials = tmp_path / "credentials"
    # Only [test-profile], NO [default] section — forces boto3 to use env vars
    credentials.write_text(
        "[test-profile]\n"
        "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
    )
    config_file = tmp_path / "config"
    config_file.write_text("")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
    # Set distinctive env credentials to verify they're actually being used
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAENVVARCREDENTIAL")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "envVarSecretAccessKey1234567890ABCDEF")


def test_get_session_empty_profile_uses_default_chain(fake_aws_credentials_env_only: None) -> None:
    session = get_session(_cfg(profile=""))
    # When profile="", boto3.Session(profile_name=None) uses the default credential chain.
    # With no [default] profile in the credentials file, boto3 must use AWS_ACCESS_KEY_ID
    # and AWS_SECRET_ACCESS_KEY env vars. Verify credentials come from env, not from
    # any profile in ~/.aws or the fake credentials file.
    credentials = session.get_credentials()
    assert credentials is not None
    assert credentials.access_key == "AKIAENVVARCREDENTIAL"


def test_env_credentials_override_stale_profile(
    fake_aws_credentials_env_only: None,
) -> None:
    # A stale config profile that isn't present must not break auth when env
    # credentials are supplied (the container case). Prior behavior raised
    # ProfileNotFound; now env credentials win.
    session = get_session(_cfg(profile="does-not-exist"))
    assert session.get_credentials().access_key == "AKIAENVVARCREDENTIAL"
