"""Évaluation : lance chaque dataset N fois et compare au diagnostic attendu.

    cd backend
    python -m eval.run_eval --runs 3                 # tous les datasets, vrai LLM
    python -m eval.run_eval --runs 1 slow-query      # un seul dataset
    python -m eval.run_eval --fake                   # sans LLM (vérifie la mécanique)

Écrit eval/results.md (tableau lisible) et eval/results.json (détail de chaque exécution).
"""

import argparse
import json
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.graph.workflow import run_analysis
from app.schemas.diagnosis import DiagnosisReport
from app.schemas.telemetry import TimeWindow
from app.services.llm.provider import get_llm
from app.tools.datasources.file_source import FileDataSource, list_datasets

EVAL_DIR = Path(__file__).resolve().parent

# Mots qui trahissent chaque catégorie de cause : repèrent une confusion (ex. S03 pris pour S01).
CATEGORY_WORDS = {
    "db_pool_exhaustion": ["pool"],
    "slow_query": ["requête lente", "slow query", "index", "requête sql lente"],
    "cpu_saturation": ["cpu"],
    "dependency_down": ["inventory", "injoignable", "unreachable", "refused"],
}


# --- Score d'une exécution (fonction pure, testée) --------------------------------------------


def score_run(report: DiagnosisReport, expected: dict[str, Any], seconds: float) -> dict[str, Any]:
    cause = (report.probable_cause or "").lower()
    found_keyword = any(word.lower() in cause for word in expected.get("cause_keywords", []))
    confusions = [
        category
        for category in expected.get("must_not_conclude", [])
        if any(word in cause for word in CATEGORY_WORDS.get(category, []))
    ]

    if "acceptable_conclusions" in expected:  # piège : le bon réflexe est de rester prudent
        correct = report.conclusion in expected["acceptable_conclusions"] and (
            report.confidence <= expected.get("max_confidence", 1.0)
        )
    else:
        correct = report.probable_cause is not None and found_keyword and not confusions

    evidence_ids = {e.id for e in report.evidences}
    cited = [
        i
        for h in report.hypotheses
        for i in h.supporting_evidence_ids + h.contradicting_evidence_ids
    ]
    observations = " ".join(e.observation for e in report.evidences)
    key = expected.get("key_evidence", [])

    return {
        "correct": correct,
        "conclusion": report.conclusion,
        "confidence": report.confidence,
        "cause": report.probable_cause,
        "confusions": confusions,
        "grounding": sum(i in evidence_ids for i in cited) / len(cited) if cited else 1.0,
        "key_evidence": sum(k["signal"] in observations for k in key) / len(key) if key else 1.0,
        "iterations": report.iteration_count,
        "seconds": round(seconds, 1),
    }


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Moyennes d'un dataset ; cohérence = part des exécutions au résultat majoritaire."""
    n = len(runs)
    majority = Counter(r["correct"] for r in runs).most_common(1)[0][1]
    return {
        "runs": n,
        "accuracy": sum(r["correct"] for r in runs) / n,
        "consistency": majority / n,
        "grounding": sum(r["grounding"] for r in runs) / n,
        "key_evidence": sum(r["key_evidence"] for r in runs) / n,
        "seconds": sum(r["seconds"] for r in runs) / n,
        "conclusions": dict(Counter(r["conclusion"] for r in runs)),
    }


# --- Exécution ---------------------------------------------------------------------------------


def evaluate(names: list[str], runs: int, settings: Settings, pause: float = 0.0) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for name in names:
        source = FileDataSource.from_name(name)
        expected = source.expected_diagnosis() or {}
        data = source.time_range()
        window = TimeWindow(start=data.start, end=data.end + timedelta(minutes=1))
        details = []
        for i in range(runs):
            print(f"[{name}] exécution {i + 1}/{runs}...", flush=True)
            started = time.perf_counter()
            report = run_analysis(source, window, get_llm(settings), incident_id=f"eval-{name}")
            details.append(score_run(report, expected, time.perf_counter() - started))
            time.sleep(pause)  # laisse le quota du fournisseur LLM se recharger
        results[name] = {"summary": summarize(details), "details": details}
    return results


def to_markdown(results: dict[str, Any], provider: str) -> str:
    lines = [
        "# Résultats de l'évaluation",
        "",
        f"Fournisseur LLM : `{provider}`",
        "",
        "| Dataset | Exéc. | Cause correcte | Cohérence | Ancrage | Preuves clés | Durée "
        "| Conclusions |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, data in results.items():
        s = data["summary"]
        conclusions = ", ".join(f"{k} ×{v}" for k, v in s["conclusions"].items())
        lines.append(
            f"| {name} | {s['runs']} | {s['accuracy']:.0%} | {s['consistency']:.0%} | "
            f"{s['grounding']:.0%} | {s['key_evidence']:.0%} | {s['seconds']:.0f} s "
            f"| {conclusions} |"
        )
    summaries = [d["summary"] for d in results.values()]
    traps = [d["summary"] for n, d in results.items() if n.startswith("trap-")]
    lines += [
        "",
        f"- **Précision globale de la cause** : {_mean(summaries, 'accuracy'):.0%}",
        f"- **Pièges correctement gérés** : {_mean(traps, 'accuracy'):.0%}" if traps else "",
        f"- **Ancrage des preuves** (ids cités qui existent) : {_mean(summaries, 'grounding'):.0%}",
        f"- **Latence moyenne** : {_mean(summaries, 'seconds'):.0f} s par analyse",
        "",
        "Cause correcte : mots-clés attendus présents et aucune cause interdite citée ; pour un "
        "piège, conclusion prudente et confiance sous le plafond attendu.",
    ]
    return "\n".join(line for line in lines if line is not None) + "\n"


def _mean(items: list[dict[str, Any]], key: str) -> float:
    return sum(i[key] for i in items) / len(items) if items else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.run_eval")
    parser.add_argument("datasets", nargs="*", help="défaut : tous les datasets")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--fake", action="store_true", help="sans LLM")
    parser.add_argument("--pause", type=float, default=0, help="secondes entre deux analyses")
    parser.add_argument("--out", type=Path, default=EVAL_DIR)
    args = parser.parse_args(argv)

    settings = Settings(llm_provider="fake") if args.fake else get_settings()
    names = args.datasets or list_datasets()
    results = evaluate(names, args.runs, settings, args.pause)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    markdown = to_markdown(results, settings.llm_provider)
    (args.out / "results.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
