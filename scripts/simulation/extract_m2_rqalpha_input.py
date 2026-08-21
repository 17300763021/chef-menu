"""Cloud entry point for the read-only M2-to-RQAlpha acceptance handoff."""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.market_data.tidb_checkpoint_store import TiDBConfig, connect
from scripts.simulation.m2_history_source import (
    extract_pinned_acceptance_input,
    write_bounded_input,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    connection = connect(TiDBConfig.from_env())
    try:
        value = extract_pinned_acceptance_input(connection)
    finally:
        connection.close()
    write_bounded_input(args.output, value)
    print({
        "accepted": True,
        "input_sha256": value.input_sha256,
        "symbol_count": len(value.symbols),
        "session_count": len(value.sessions),
        "bar_count": len(value.bars),
        "authoritative": value.authoritative,
        "simulation_orders_allowed": value.simulation_orders_allowed,
    })


if __name__ == "__main__":
    main()
