"""Measure the translation-QC detectors against the real library, read-only.

**Why this exists.** Every threshold in `translators/qc.py` decides whether a chapter the
user already owns gets flagged as broken. A number picked by intuition here is not a small
mistake: set the diacritic floor too high and every cultivation novel is "hỏng"; set the CJK
ceiling as a count instead of a ratio and 69 chapters with one stray glyph are condemned.
So the constants are *derived* — this script is what derives them, and the rule is the one
`chapter_titles.py` already sets for its own heuristics: re-run this and read the output
before nudging any constant.

It opens every `chapters.db` under the configured library with `mode=ro` and never writes
anything. Chapter text stays on the machine: only aggregate distributions and short
excerpts of the chapters that would be FLAGGED are printed, because those are the ones a
human has to judge.

    python scripts/qc_calibrate.py                 # the configured library
    python scripts/qc_calibrate.py /path/to/lib    # somewhere else
    python scripts/qc_calibrate.py --flagged 40    # show more flagged excerpts

Read the output like this:

* every "would flag" line is a chapter the shipped detector calls broken. Open a few. If
  any of them reads fine, the threshold is wrong — not the chapter;
* the percentile table is the safety margin. A threshold sitting between p0 of the good
  population and the flagged cases has room; one sitting on top of p1 does not.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from noveltrans.translators.qc import (  # noqa: E402
    MAX_CJK_RATIO,
    MAX_ENGLISH_WORD_RATE,
    MIN_CJK_CHARS,
    MIN_DIACRITIC_RATIO,
    QC_OK,
    check_translation,
    cjk_ratio,
    diacritic_ratio,
    english_word_rate,
    vietnamese_word_rate,
)

SIGNALS = {
    "diacritic_ratio": diacritic_ratio,
    "english_word_rate": english_word_rate,
    "cjk_ratio": cjk_ratio,
    "vietnamese_word_rate": vietnamese_word_rate,
}


def percentile(values: list[float], q: float) -> float:
    """The `q`-th percentile (0..100) of `values`, nearest-rank. No numpy dependency."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q / 100 * (len(ordered) - 1))))
    return ordered[index]


def iter_chapters(library: Path):
    """Yield (project_dir, idx, title, source, translated, engine) for every translation."""
    for db_path in sorted(library.glob("*/chapters.db")):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:  # a half-written or locked project must not stop the run
            print(f"  ! bỏ qua {db_path.parent.name}: {exc}", file=sys.stderr)
            continue
        try:
            rows = conn.execute(
                "SELECT idx, title, content, translated_title, translated, translator "
                "FROM chapters WHERE translated != '' ORDER BY idx"
            ).fetchall()
        except sqlite3.Error as exc:
            print(f"  ! bỏ qua {db_path.parent.name}: {exc}", file=sys.stderr)
            continue
        finally:
            conn.close()
        for row in rows:
            yield (
                db_path.parent.name,
                row["idx"],
                row["translated_title"] or row["title"] or "",
                row["content"] or "",
                row["translated"],
                row["translator"] or "?",
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", nargs="?", default="", help="library folder (default: configured)")
    parser.add_argument("--flagged", type=int, default=25, help="how many flagged chapters to show")
    args = parser.parse_args()

    if args.library:
        library = Path(args.library).expanduser()
    else:
        from noveltrans.config import AppConfig

        library = AppConfig().library_dir
    if not library.is_dir():
        print(f"Không tìm thấy thư viện: {library}", file=sys.stderr)
        return 1

    print(f"Thư viện: {library}\n")
    measured: dict[str, list[float]] = {name: [] for name in SIGNALS}
    flagged: list[tuple] = []
    engines: dict[str, int] = {}
    total = 0

    for project, idx, title, source, translated, engine in iter_chapters(library):
        total += 1
        engines[engine] = engines.get(engine, 0) + 1
        for name, fn in SIGNALS.items():
            measured[name].append(fn(translated))
        # The judge is a live model, so calibration covers the deterministic layer only.
        verdict = check_translation(source, title, translated)
        if verdict.code != QC_OK:
            flagged.append((project, idx, engine, verdict, translated))

    if not total:
        print("Không có chương nào đã dịch.", file=sys.stderr)
        return 1

    print(f"{total} chương đã dịch, {len(engines)} engine:")
    for engine, count in sorted(engines.items(), key=lambda kv: -kv[1]):
        print(f"  {count:6d}  {engine}")

    print("\nPhân bố tín hiệu (trên toàn bộ chương đã dịch):")
    print(f"  {'tín hiệu':22s} {'min':>8s} {'p1':>8s} {'p50':>8s} {'p99':>8s} {'max':>8s}")
    for name, values in measured.items():
        print(
            f"  {name:22s} {min(values):8.4f} {percentile(values, 1):8.4f} "
            f"{percentile(values, 50):8.4f} {percentile(values, 99):8.4f} {max(values):8.4f}"
        )

    print("\nNgưỡng đang dùng và khoảng an toàn so với dữ liệu thật:")
    print(
        f"  MIN_DIACRITIC_RATIO   = {MIN_DIACRITIC_RATIO:.2f}  "
        f"(thấp nhất thật: {min(measured['diacritic_ratio']):.4f})"
    )
    print(
        f"  MAX_ENGLISH_WORD_RATE = {MAX_ENGLISH_WORD_RATE:.2f}  "
        f"(cao nhất thật: {max(measured['english_word_rate']):.4f})"
    )
    print(
        f"  MAX_CJK_RATIO         = {MAX_CJK_RATIO:.3f} và MIN_CJK_CHARS = {MIN_CJK_CHARS}  "
        f"(cao nhất thật: {max(measured['cjk_ratio']):.4f})"
    )

    # vietnamese_word_rate is measured but deliberately NOT thresholded: the lowest scores
    # in a real library belong to good tiên hiệp chapters, whose genre IS Hán-Việt. The
    # bottom of this list is the evidence — read it before anyone proposes a threshold.
    print("\n10 chương có vietnamese_word_rate THẤP NHẤT (kiểm chứng: đây phải là chương TỐT):")
    lowest = sorted(
        (
            (vietnamese_word_rate(text), project, idx, engine, text)
            for project, idx, _t, _s, text, engine in iter_chapters(library)
        ),
        key=lambda row: row[0],
    )[:10]
    for rate, project, idx, engine, text in lowest:
        excerpt = " ".join(text.split())[:110]
        print(f"  {rate:.3f}  {project} #{idx} [{engine}]  {excerpt}…")

    print(f"\n{len(flagged)} chương bị ĐÁNH DẤU LỖI bởi lớp heuristic ({len(flagged)/total:.2%}):")
    for project, idx, engine, verdict, text in flagged[: args.flagged]:
        excerpt = " ".join(text.split())[:110]
        print(f"  [{verdict.code}] {project} #{idx} [{engine}] — {verdict.reason}")
        print(f"      {excerpt}…")
    if len(flagged) > args.flagged:
        print(f"  … còn {len(flagged) - args.flagged} chương nữa (dùng --flagged N để xem thêm)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
