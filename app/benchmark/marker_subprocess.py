from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


def _safe_image_map(image_map: dict, output_dir: Path) -> dict[str, str]:
    if not image_map:
        return {}
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[str, str] = {}
    for name, value in image_map.items():
        safe_name = str(name).replace("/", "_").replace("\\", "_")
        target = images_dir / safe_name
        try:
            if hasattr(value, "save"):
                if not target.suffix:
                    target = target.with_suffix(".png")
                value.save(target)
                saved[str(name)] = str(target.relative_to(output_dir))
            elif isinstance(value, (bytes, bytearray)):
                target.write_bytes(bytes(value))
                saved[str(name)] = str(target.relative_to(output_dir))
            elif isinstance(value, (str, Path)) and Path(value).exists():
                saved[str(name)] = str(value)
        except Exception:
            continue
    return saved


async def _run(input_path: str, output_dir: str) -> int:
    from app.services.marker_ocr import extract_markdown_with_subretry

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    # force_ocr=True bắt Surya texify chạy → markdown có LaTeX `$...$`.
    # Subprocess fallback chạy khi không có marker_single CLI; cần sync với
    # CLI path (đã set --force_ocr) và production pipeline cho STEM.
    extracted = await extract_markdown_with_subretry(input_path, force_ocr=True)
    markdown = str(extracted.get("text") or "")
    image_map = _safe_image_map(extracted.get("image_map") or {}, out)
    raw = {k: v for k, v in extracted.items() if k != "image_map"}
    raw["image_map"] = image_map
    (out / "output.md").write_text(markdown, encoding="utf-8")
    (out / "marker_raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if markdown.strip() else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Marker OCR in an isolated subprocess.")
    parser.add_argument("input")
    parser.add_argument("output")
    args = parser.parse_args()
    return asyncio.run(_run(args.input, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
