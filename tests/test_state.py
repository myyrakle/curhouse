import json
from pathlib import Path

from curhouse.state import (
    CURRENT_SCHEMA_VERSION,
    LEGACY_V1_SOURCE_NAME,
    ManifestRecord,
    State,
    load_state,
    save_state,
)


def _mk_record(period: str = "2026-04", rows: int = 1000) -> ManifestRecord:
    return ManifestRecord(
        etag=f'"{period}-etag"',
        last_modified=f"{period}-15T03:21:00Z",
        last_synced_at=f"{period}-15T04:00:00Z",
        row_count=rows,
    )


def test_load_state_missing_returns_empty(tmp_path: Path) -> None:
    state = load_state(tmp_path / "nope.json")
    assert state.schema_version == CURRENT_SCHEMA_VERSION
    assert state.sources == {}
    assert state.ddl_hash is None


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    state = State(
        schema_version=CURRENT_SCHEMA_VERSION,
        table_created_at="2026-05-27T10:00:00Z",
        ddl_hash="sha256:abc",
        sources={
            "dev": {"2026-04": _mk_record("2026-04", 1000)},
            "qa":  {"2026-09": _mk_record("2026-09", 42)},
        },
    )
    save_state(p, state)

    loaded = load_state(p)
    assert loaded == state


def test_save_is_atomic(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    save_state(p, State())
    assert not (tmp_path / "s.json.tmp").exists()
    assert p.exists()


def test_saved_file_shape(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    state = State(sources={"dev": {"2026-04": _mk_record("2026-04", 1)}})
    save_state(p, state)
    parsed = json.loads(p.read_text())
    assert parsed["schema_version"] == CURRENT_SCHEMA_VERSION
    assert parsed["sources"]["dev"]["manifests"]["2026-04"]["row_count"] == 1


def test_v1_state_auto_migrates_to_v2(tmp_path: Path) -> None:
    # 예전 v1 state 파일 (flat manifests, 소스 개념 없음)
    p = tmp_path / "s.json"
    p.write_text(json.dumps({
        "schema_version": 1,
        "table_created_at": "2026-07-10T05:48:12Z",
        "ddl_hash": "sha256:abc",
        "manifests": {
            "2026-07": {
                "etag": '"jul"',
                "last_modified": "2026-08-02T02:13:09+00:00",
                "last_synced_at": "2026-08-02T23:00:19+00:00",
                "row_count": 1030078,
            },
            "2026-08": {
                "etag": '"aug"',
                "last_modified": "2026-08-04T22:45:44+00:00",
                "last_synced_at": "2026-08-04T23:00:10+00:00",
                "row_count": 124117,
            },
        },
    }))

    loaded = load_state(p)
    # 자동 승격: 예전 flat 데이터 → sources["dev"] (LEGACY_V1_SOURCE_NAME)
    assert loaded.schema_version == CURRENT_SCHEMA_VERSION
    assert LEGACY_V1_SOURCE_NAME == "dev"
    assert set(loaded.sources.keys()) == {"dev"}
    assert loaded.sources["dev"]["2026-07"].row_count == 1030078
    assert loaded.sources["dev"]["2026-08"].row_count == 124117
    assert loaded.ddl_hash == "sha256:abc"
    assert loaded.table_created_at == "2026-07-10T05:48:12Z"


def test_v1_state_with_no_manifests_becomes_empty_sources(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"schema_version": 1, "manifests": {}}))
    loaded = load_state(p)
    assert loaded.sources == {}


def test_manifests_for_returns_empty_when_source_absent() -> None:
    state = State(sources={"dev": {"2026-08": _mk_record()}})
    assert state.manifests_for("qa") == {}
    assert "2026-08" in state.manifests_for("dev")
