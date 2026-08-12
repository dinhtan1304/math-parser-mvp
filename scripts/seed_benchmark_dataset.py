"""Helper script — populate benchmark_dataset từ một thư mục file thực.

Usage:
    python scripts/seed_benchmark_dataset.py --source uploads --category vn_scanned --pattern "Kiêm tra*.pdf" --subject toan --grade 10 --limit 2

Hoặc chạy interactive (lấy từ uploads/ tự pick):
    python scripts/seed_benchmark_dataset.py --auto-pick uploads --limit 12

`--auto-pick` sẽ tự phân loại file theo tên + thử fill 6 category với 2 file mỗi loại.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.benchmark import dataset_manager  # noqa: E402


# Heuristic auto-classification từ filename
def _auto_classify(name: str) -> tuple[str, str]:
    """Trả (category, subject) suy ra từ tên file."""
    n = name.lower()
    # STEM markers
    if any(k in n for k in ["toan", "hsg", "min max", "phan loai", "dai so", "hinh"]):
        # STEM text-layer hay scanned?
        if "latex" in n:
            return "stem_text_layer", "toan"
        return "stem_scanned", "toan"
    if any(k in n for k in ["ielts", "cambridge", "english", "anh"]):
        return "text_heavy", "tieng-anh"
    if "kiem tra" in n or "kiêm" in n:
        return "vn_scanned", "toan"
    return "edge_case", "unknown"


def cmd_add(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    if not source.exists():
        print(f"Source not found: {source}", file=sys.stderr)
        return 2
    files = sorted(source.glob(args.pattern))[: args.limit]
    if not files:
        print(f"No files matching {args.pattern} in {source}", file=sys.stderr)
        return 1
    print(f"Adding {len(files)} files as category={args.category}, subject={args.subject}")
    for f in files:
        try:
            entry = dataset_manager.add_file(
                source_path=f,
                original_filename=f.name,
                category=args.category,
                subject=args.subject,
                grade=args.grade,
                anonymized=args.anonymized,
            )
            print(f"  + {entry.file_id} ({entry.page_count}p, {entry.file_size_bytes//1024}KB) ← {f.name}")
        except Exception as exc:
            print(f"  ! FAILED {f.name}: {exc}", file=sys.stderr)
    return 0


def cmd_auto_pick(args: argparse.Namespace) -> int:
    source = Path(args.source).resolve()
    if not source.exists():
        print(f"Source not found: {source}", file=sys.stderr)
        return 2
    quotas: dict[str, int] = {
        "vn_scanned": 2,
        "stem_text_layer": 2,
        "stem_scanned": 2,
        "layout_heavy": 1,
        "text_heavy": 2,
        "edge_case": 1,
    }
    if args.limit and args.limit < sum(quotas.values()):
        # Scale down proportionally
        ratio = args.limit / sum(quotas.values())
        quotas = {k: max(1, int(v * ratio)) for k, v in quotas.items()}
    filled: dict[str, int] = {k: 0 for k in quotas}
    seen: set[str] = set()  # dedupe by sha or basename stem
    total = 0
    for f in sorted(source.glob("**/*.pdf")):
        stem_key = f.stem.split("_", 1)[-1] if "_" in f.stem else f.stem
        if stem_key in seen:
            continue
        cat, subj = _auto_classify(f.name)
        if filled.get(cat, 0) >= quotas.get(cat, 0):
            continue
        try:
            entry = dataset_manager.add_file(
                source_path=f,
                original_filename=f.name,
                category=cat,
                subject=subj,
                grade=10,
                anonymized=True,
            )
            seen.add(stem_key)
            filled[cat] += 1
            total += 1
            print(f"  [{cat:18s}] {entry.file_id} ({entry.page_count}p) ← {f.name}")
        except Exception as exc:
            print(f"  ! skip {f.name}: {exc}", file=sys.stderr)
        if all(filled[k] >= quotas[k] for k in quotas):
            break
    print(f"\nDone. Added {total} files. Distribution:")
    for k, v in quotas.items():
        print(f"  {k:18s} {filled[k]}/{v}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    del args
    entries = dataset_manager.list_entries()
    if not entries:
        print("Dataset empty. Use `add` or `auto-pick` to populate.")
        return 0
    stats = dataset_manager.stats()
    print(f"Dataset: {stats['total_files']} files, {stats['total_pages']} pages, {stats['total_bytes']//1024//1024} MB")
    print(f"By category: {stats['by_category']}")
    print(f"By subject:  {stats['by_subject']}")
    print()
    for e in entries:
        print(f"  {e.file_id:40s} {e.category:18s} {e.subject:12s} g{e.grade or '?':3} {e.page_count:3d}p")
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    if not args.yes:
        print("Refusing to clear without --yes flag.", file=sys.stderr)
        return 2
    entries = dataset_manager.list_entries()
    removed = 0
    for e in entries:
        if dataset_manager.remove_entry(e.file_id):
            removed += 1
    print(f"Removed {removed} entries.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed benchmark dataset from real files.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Add files matching pattern with explicit category")
    p_add.add_argument("--source", required=True, help="Source folder")
    p_add.add_argument("--pattern", default="*.pdf", help="Glob pattern (default: *.pdf)")
    p_add.add_argument("--category", required=True, choices=sorted(dataset_manager.VALID_CATEGORIES))
    p_add.add_argument("--subject", default="unknown")
    p_add.add_argument("--grade", type=int, default=10)
    p_add.add_argument("--limit", type=int, default=10)
    p_add.add_argument("--anonymized", action="store_true", default=True)
    p_add.set_defaults(func=cmd_add)

    p_pick = sub.add_parser("auto-pick", help="Auto-classify and add files from a folder")
    p_pick.add_argument("--source", required=True)
    p_pick.add_argument("--limit", type=int, default=12)
    p_pick.set_defaults(func=cmd_auto_pick)

    p_list = sub.add_parser("list", help="List dataset entries")
    p_list.set_defaults(func=cmd_list)

    p_clear = sub.add_parser("clear", help="Remove all entries (DESTRUCTIVE)")
    p_clear.add_argument("--yes", action="store_true")
    p_clear.set_defaults(func=cmd_clear)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
