"""OCR benchmark engine registry — lazy-loaded.

Engines are imported on first access (not at package import) so that pulling in
one engine never drags in another's heavy/conflicting deps. This matters on
GPU: several engines (chandra/dots/olmocr) ``import torch`` at module load, and
torch loads its own CUDA/cuDNN DLLs. If torch loads *before* paddle in the same
process, paddle's ``cudnn_cnn64_9.dll`` fails to resolve (``WinError 127``). The
production /upload path only uses ``mineru`` + ``paddle``; lazy access keeps
torch out of that process so PaddleOCR-VL loads cleanly.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# class name → submodule (under app.benchmark.engines)
_CLASS_TO_MODULE = {
    "MarkItDownEngine": "markitdown_engine",
    "MarkerEngine": "marker_engine",
    "MinerUEngine": "mineru_engine",
    "ChandraEngine": "chandra_engine",
    "OlmOcrEngine": "olmocr_engine",
    "PaddleEngine": "paddle_engine",
    "MathpixEngine": "mathpix_engine",
    "GraniteDoclingEngine": "granite_docling_engine",
    "DotsOcrEngine": "dots_ocr_engine",
}

# benchmark key → class name (drives ENGINE_CLASSES)
_KEY_TO_CLASS = {
    "markitdown": "MarkItDownEngine",
    "marker": "MarkerEngine",
    "mineru": "MinerUEngine",
    "chandra": "ChandraEngine",
    "olmocr": "OlmOcrEngine",
    "paddle": "PaddleEngine",
    "mathpix": "MathpixEngine",
    "granite-docling": "GraniteDoclingEngine",
    "dots": "DotsOcrEngine",
}

if TYPE_CHECKING:  # static imports for type-checkers / IDEs only (not at runtime)
    from app.benchmark.engines.chandra_engine import ChandraEngine
    from app.benchmark.engines.dots_ocr_engine import DotsOcrEngine
    from app.benchmark.engines.granite_docling_engine import GraniteDoclingEngine
    from app.benchmark.engines.marker_engine import MarkerEngine
    from app.benchmark.engines.markitdown_engine import MarkItDownEngine
    from app.benchmark.engines.mathpix_engine import MathpixEngine
    from app.benchmark.engines.mineru_engine import MinerUEngine
    from app.benchmark.engines.olmocr_engine import OlmOcrEngine
    from app.benchmark.engines.paddle_engine import PaddleEngine


def _load_class(class_name: str) -> Any:
    module = importlib.import_module(f"app.benchmark.engines.{_CLASS_TO_MODULE[class_name]}")
    return getattr(module, class_name)


class _LazyEngineClasses(dict):
    """``ENGINE_CLASSES`` that imports each engine class on first lookup.

    Behaves like the old ``{key: Class}`` dict but defers the (heavy) import so
    e.g. ``ENGINE_CLASSES["paddle"]`` never triggers a torch import.
    """

    def __missing__(self, key: str) -> Any:
        if key not in _KEY_TO_CLASS:
            raise KeyError(key)
        cls = _load_class(_KEY_TO_CLASS[key])
        self[key] = cls
        return cls

    def get(self, key: object, default: Any = None) -> Any:
        # dict.get does NOT trigger __missing__, so override to keep lazy load.
        if key in _KEY_TO_CLASS:
            return self[key]
        return default

    def __iter__(self):
        return iter(_KEY_TO_CLASS)

    def keys(self):
        return _KEY_TO_CLASS.keys()

    def items(self):
        return [(k, self[k]) for k in _KEY_TO_CLASS]

    def values(self):
        return [self[k] for k in _KEY_TO_CLASS]

    def __contains__(self, key: object) -> bool:
        return key in _KEY_TO_CLASS


ENGINE_CLASSES = _LazyEngineClasses()


def __getattr__(name: str) -> Any:
    """PEP 562 lazy access: ``from app.benchmark.engines import PaddleEngine``."""
    if name in _CLASS_TO_MODULE:
        return _load_class(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
