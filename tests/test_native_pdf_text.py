import sys
from types import SimpleNamespace

from app.services.native_pdf import extract_native_pdf_markdown


class FakeDoc:
    def __init__(self, pages):
        self.pages = pages

    def __len__(self):
        return len(self.pages)

    def __iter__(self):
        return iter(self.pages)

    def close(self):
        pass


class FakePage:
    def __init__(self, text):
        self.text = text

    def get_text(self, kind):
        return self.text


def test_extract_native_pdf_markdown_is_fast_text_path(monkeypatch, tmp_path):
    fake_fitz = SimpleNamespace(
        open=lambda path: FakeDoc(
            [
                FakePage("Cau 1. Tinh A = 1/2. " * 8),
                FakePage("Cau 2. Chung minh. " * 8),
            ]
        )
    )
    monkeypatch.setitem(sys.modules, "fitz", fake_fitz)
    path = tmp_path / "text.pdf"
    path.write_bytes(b"%PDF mocked")

    result = extract_native_pdf_markdown(path)

    assert result["method"] == "native-pdf-text"
    assert result["page_count"] == 2
    assert "Cau 1" in result["text"]
    assert "Cau 2" in result["text"]
    assert result["requires_latex_normalization"] is True
