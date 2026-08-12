import sys
from types import SimpleNamespace

from app.services.pdf_detector import analyze_pdf_for_ocr


class FakePage:
    def __init__(self, *, text: str = "", image: bool = False):
        self._text = text
        self._image = image
        self.rect = SimpleNamespace(width=300, height=400)

    def get_text(self, kind):
        if kind == "text":
            return self._text
        blocks = []
        if self._text:
            blocks.append({"type": 0, "lines": [{"spans": [{"text": self._text}]}]})
        if self._image:
            blocks.append({"type": 1, "bbox": [0, 0, 300, 400]})
        return {"blocks": blocks}

    def get_images(self, full=True):
        return [(1,)] if self._image else []


class FakeDoc:
    def __init__(self, pages):
        self.pages = pages

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def close(self):
        pass


def _install_fake_fitz(monkeypatch, pages):
    fake_fitz = SimpleNamespace(open=lambda path: FakeDoc(pages))
    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)


def test_pdf_detector_classifies_text_pdf(monkeypatch, tmp_path):
    _install_fake_fitz(
        monkeypatch,
        [FakePage(text="Cau 1. " + "Day la PDF co text layer. " * 20)],
    )
    path = tmp_path / "text.pdf"
    path.write_bytes(b"%PDF mocked")

    result = analyze_pdf_for_ocr(path)

    assert result.pdf_kind == "text_pdf"
    assert result.recommended_ocr_mode == "text"
    assert result.has_text_layer is True


def test_pdf_detector_classifies_scanned_pdf(monkeypatch, tmp_path):
    _install_fake_fitz(monkeypatch, [FakePage(image=True)])
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF mocked")

    result = analyze_pdf_for_ocr(path)

    assert result.pdf_kind == "scan_pdf"
    assert result.recommended_ocr_mode == "ocr"
    assert result.scan_page_ratio == 1


def test_pdf_detector_classifies_mixed_pdf(monkeypatch, tmp_path):
    _install_fake_fitz(
        monkeypatch,
        [
            FakePage(text="Cau 1. " + "PDF text layer. " * 20),
            FakePage(image=True),
        ],
    )
    path = tmp_path / "mixed.pdf"
    path.write_bytes(b"%PDF mocked")

    result = analyze_pdf_for_ocr(path)

    assert result.pdf_kind == "mixed_pdf"
    assert result.recommended_ocr_mode == "mixed"
