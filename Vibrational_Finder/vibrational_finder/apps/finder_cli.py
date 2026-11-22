from __future__ import annotations

import argparse

from finder_core.data_sources import SourceQuery
from finder_core.export import write_match_table
from finder_core.models import SignalKind
from vibrational_finder.io.xy_loader import load_raman_spectrum
from vibrational_finder.matching import MatchingOptions, rank_candidates
from vibrational_finder.metadata import APP_NAME
from vibrational_finder.services import UserLibrarySource


def run_single_kind(default_kind: SignalKind = SignalKind.RAMAN) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--experiment", required=True, help="Experimental Raman spectrum")
    parser.add_argument("--kind", choices=["raman"], default=SignalKind.RAMAN.value)
    parser.add_argument("--library", required=True, help="CSV manifest with reference spectra")
    parser.add_argument("--query", default="", help="Optional text filter for the library")
    parser.add_argument("--tolerance", type=float, default=12.0, help="Band match tolerance in cm-1")
    parser.add_argument("--export", default="", help="Optional CSV output path")
    args = parser.parse_args()

    if default_kind not in {SignalKind.RAMAN, SignalKind.UNKNOWN}:
        parser.error("Only Raman spectra are supported.")
    try:
        observed = load_raman_spectrum(args.experiment)
        library = UserLibrarySource(args.library)
        candidates = library.load_candidates(SourceQuery(text=args.query, kind=SignalKind.RAMAN))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    results = rank_candidates(observed, candidates, MatchingOptions(tolerance_cm1=args.tolerance))
    rows = [
        {
            "source": result.candidate.source,
            "entry": result.candidate.entry_id,
            "formula": result.candidate.formula,
            "compound": result.candidate.name,
            "kind": result.candidate.kind.value,
            "match": f"{result.score.combined:.1f}",
            "position": f"{result.score.position:.1f}",
            "intensity": f"{result.score.intensity:.1f}",
            "correlation": f"{result.score.correlation:.1f}",
            "coverage": f"{result.score.coverage:.1f}",
            "x_shift_cm1": f"{result.score.x_shift:.1f}",
            "bands": f"{result.score.matched_features}/{result.score.total_features}",
        }
        for result in results
    ]
    if args.export:
        write_match_table(args.export, rows)
    for row in rows[:25]:
        print(
            f"{row['match']:>5}%  {row['source']}:{row['entry']}  "
            f"{row['formula']:<12} {row['compound']}  "
            f"corr={row['correlation']} bands={row['bands']} shift={row['x_shift_cm1']}"
        )
    return 0


def main() -> int:
    return run_single_kind()


if __name__ == "__main__":
    raise SystemExit(main())
