from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from curhouse.config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curhouse",
        description="AWS CUR 2.0 to local ClickHouse loader",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", default="config.toml", help="path to config.toml")
        sp.add_argument("--verbose", "-v", action="store_true")

    sp_setup = sub.add_parser("setup", help="provision S3 bucket and CUR export")
    _add_common(sp_setup)
    sp_setup.add_argument(
        "--source",
        default=None,
        help="source name to set up (required when multiple sources defined)",
    )

    sp_sync = sub.add_parser("sync", help="sync changed billing periods")
    _add_common(sp_sync)
    sp_sync.add_argument(
        "--only-period",
        default=None,
        help="force reload a single billing period (YYYY-MM)",
    )
    sp_sync.add_argument(
        "--only-source",
        default=None,
        help="only sync one source (dev/qa/prod). Defaults to all sources.",
    )

    sp_status = sub.add_parser("status", help="show last sync state")
    _add_common(sp_status)

    sp_init = sub.add_parser("init-schema", help="create ClickHouse table only")
    _add_common(sp_init)
    sp_init.add_argument(
        "--source",
        default=None,
        help="source whose manifest to derive DDL from (required if multiple)",
    )

    return parser


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    config_path = Path(args.config)
    try:
        cfg = load_config(config_path)
    except FileNotFoundError:
        print(
            f"error: config file not found: {config_path}\n"
            "  Copy config.example.toml to config.toml and edit it.",
            file=sys.stderr,
        )
        return 1
    except ValueError as e:
        print(f"error: invalid config: {e}", file=sys.stderr)
        return 1

    # 서브커맨드별 핸들러 (commands.py는 Task 11에서 추가)
    from curhouse import commands

    handler = {
        "setup": commands.cmd_setup,
        "sync": commands.cmd_sync,
        "status": commands.cmd_status,
        "init-schema": commands.cmd_init_schema,
    }[args.command]
    return handler(cfg, args)
