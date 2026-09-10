from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 소스가 도입되기 전(v1) state는 dev 계정 하나만 있었다.
# v1 → v2 마이그레이션 시 flat manifests를 이 소스명 아래로 옮긴다.
LEGACY_V1_SOURCE_NAME = "dev"

CURRENT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ManifestRecord:
    etag: str
    last_modified: str
    last_synced_at: str
    row_count: int


@dataclass(frozen=True)
class State:
    schema_version: int = CURRENT_SCHEMA_VERSION
    table_created_at: str | None = None
    ddl_hash: str | None = None
    # sources[<source_name>][<billing_period>] = ManifestRecord
    sources: dict[str, dict[str, ManifestRecord]] = field(default_factory=dict)

    def manifests_for(self, source: str) -> dict[str, ManifestRecord]:
        return self.sources.get(source, {})


def _records_from_json(raw: dict) -> dict[str, ManifestRecord]:
    return {period: ManifestRecord(**rec) for period, rec in raw.items()}


def load_state(path: Path | str) -> State:
    path = Path(path)
    if not path.exists():
        return State()

    data = json.loads(path.read_text())
    version = int(data.get("schema_version", 1))

    if version == 1:
        # v1: flat top-level "manifests" → sources[LEGACY_V1_SOURCE_NAME]
        legacy = _records_from_json(data.get("manifests", {}))
        sources = {LEGACY_V1_SOURCE_NAME: legacy} if legacy else {}
    else:
        sources = {
            source_name: _records_from_json(entry.get("manifests", {}))
            for source_name, entry in data.get("sources", {}).items()
        }

    return State(
        schema_version=CURRENT_SCHEMA_VERSION,  # 저장 시엔 항상 v2로 승격
        table_created_at=data.get("table_created_at"),
        ddl_hash=data.get("ddl_hash"),
        sources=sources,
    )


def save_state(path: Path | str, state: State) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "table_created_at": state.table_created_at,
        "ddl_hash": state.ddl_hash,
        "sources": {
            source_name: {
                "manifests": {
                    period: asdict(rec) for period, rec in manifests.items()
                }
            }
            for source_name, manifests in state.sources.items()
        },
    }

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)
