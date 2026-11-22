"""Ligne de commande : analyse un dataset et affiche le rapport JSON.

python -m app.cli run ../datasets/db-pool-exhaustion
python -m app.cli run ../datasets/db-pool-exhaustion --fake   # sans LLM (résumés de secours)
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

from app.config import Settings, get_settings
from app.graph.workflow import run_analysis
from app.schemas.telemetry import TimeWindow
from app.services.llm.provider import get_llm
from app.tools.datasources.file_source import FileDataSource


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="analyse un dossier de dataset")
    run.add_argument("dataset", type=Path)
    run.add_argument("--fake", action="store_true", help="n'appelle pas de vrai LLM")
    args = parser.parse_args(argv)

    source = FileDataSource(args.dataset)
    data = source.time_range()
    # Une minute de marge : les derniers spans se terminent après leur début.
    window = TimeWindow(start=data.start, end=data.end + timedelta(minutes=1))
    settings = Settings(llm_provider="fake") if args.fake else get_settings()

    report = run_analysis(source, window, get_llm(settings), incident_id=source.name)
    # Octets UTF-8 : la console Windows ne doit pas changer l'encodage des accents.
    sys.stdout.buffer.write(report.model_dump_json(indent=2).encode("utf-8") + b"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
