import os
import subprocess

from app.benchmark.engines import base
import app.benchmark.engines.marker_engine as marker_engine
import app.benchmark.engines.mineru_engine as mineru_engine
from app.benchmark.engines.marker_engine import MarkerEngine
from app.benchmark.engines.mineru_engine import MinerUEngine


def test_benchmark_command_timeout_stays_below_endpoint_timeout(monkeypatch):
    monkeypatch.setenv("OCR_BENCHMARK_PER_ENGINE_TIMEOUT", "600")
    monkeypatch.delenv("OCR_BENCHMARK_TIMEOUT_SECONDS", raising=False)

    assert base.benchmark_command_timeout_seconds() == 570


def test_marker_benchmark_uses_cli_latex_path_by_default(monkeypatch, tmp_path):
    captured = {}

    def fake_run_command_template(*, engine, template, file_path, output_dir, timeout=None):
        captured["engine"] = engine
        captured["template"] = template
        return "Cau 1: $x^2$", {"command": template}, 123

    monkeypatch.delenv("MARKER_CMD", raising=False)
    monkeypatch.delenv("OCR_BENCHMARK_ALLOW_INPROCESS_MARKER", raising=False)
    monkeypatch.setattr(marker_engine, "package_available", lambda package: package == "marker")
    monkeypatch.setattr(marker_engine, "command_available", lambda *commands: r"E:\fake\marker_single.exe")
    monkeypatch.setattr(marker_engine, "run_command_template", fake_run_command_template)

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MarkerEngine().run(sample, tmp_path / "marker")

    assert result.status == "success"
    assert captured["engine"] == "marker"
    assert "marker_single.exe" in captured["template"]
    assert "--output_dir" in captured["template"]
    assert "--disable_image_extraction" in captured["template"]
    assert "--disable_ocr" not in captured["template"]
    assert "--force_ocr" in captured["template"]


def test_marker_benchmark_uses_native_fast_path_for_text_pdf(monkeypatch, tmp_path):
    calls = []

    monkeypatch.delenv("MARKER_CMD", raising=False)
    monkeypatch.setenv("MARKER_BENCHMARK_ALLOW_NATIVE", "1")
    monkeypatch.setattr(marker_engine, "recommended_ocr_mode", lambda file_path: "text")
    monkeypatch.setattr(
        marker_engine,
        "extract_native_pdf_markdown",
        lambda file_path: {"text": "Cau 1 native", "method": "native-pdf-text", "page_count": 1, "image_map": {}},
    )
    monkeypatch.setattr(
        marker_engine,
        "run_command_template",
        lambda **kwargs: calls.append(kwargs) or ("", {}, 0),
    )

    sample = tmp_path / "text.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MarkerEngine().run(sample, tmp_path / "marker")

    assert result.status == "success"
    assert result.markdown == "Cau 1 native"
    assert result.raw["mode"] == "text"
    assert calls == []


def test_marker_benchmark_can_enable_ocr_for_scanned_pdf(monkeypatch, tmp_path):
    captured = {}

    def fake_run_command_template(*, engine, template, file_path, output_dir, timeout=None):
        captured["template"] = template
        return "Cau 1: $x^2$", {"command": template}, 123

    monkeypatch.delenv("MARKER_CMD", raising=False)
    monkeypatch.setenv("MARKER_BENCHMARK_DISABLE_OCR", "0")
    monkeypatch.setattr(marker_engine, "package_available", lambda package: package == "marker")
    monkeypatch.setattr(marker_engine, "command_available", lambda *commands: r"E:\fake\marker_single.exe")
    monkeypatch.setattr(marker_engine, "run_command_template", fake_run_command_template)

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MarkerEngine().run(sample, tmp_path / "marker")

    assert result.status == "success"
    assert "--disable_ocr" not in captured["template"]


def test_marker_benchmark_auto_enables_ocr_for_detected_scan(monkeypatch, tmp_path):
    captured = {}

    def fake_run_command_template(*, engine, template, file_path, output_dir, timeout=None):
        captured["template"] = template
        return "Cau 1: $x^2$", {"command": template}, 123

    monkeypatch.delenv("MARKER_CMD", raising=False)
    monkeypatch.delenv("MARKER_BENCHMARK_DISABLE_OCR", raising=False)
    monkeypatch.setattr(marker_engine, "package_available", lambda package: package == "marker")
    monkeypatch.setattr(marker_engine, "command_available", lambda *commands: r"E:\fake\marker_single.exe")
    monkeypatch.setattr(marker_engine, "run_command_template", fake_run_command_template)
    monkeypatch.setattr(
        marker_engine,
        "recommended_ocr_mode",
        lambda file_path: "ocr",
    )

    sample = tmp_path / "scan.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MarkerEngine().run(sample, tmp_path / "marker")

    assert result.status == "success"
    assert "--disable_ocr" not in captured["template"]


def test_marker_benchmark_auto_env_uses_detector(monkeypatch, tmp_path):
    captured = {}

    def fake_run_command_template(*, engine, template, file_path, output_dir, timeout=None):
        captured["template"] = template
        return "Cau 1: $x^2$", {"command": template}, 123

    monkeypatch.delenv("MARKER_CMD", raising=False)
    monkeypatch.setenv("MARKER_BENCHMARK_DISABLE_OCR", "auto")
    monkeypatch.setattr(marker_engine, "package_available", lambda package: package == "marker")
    monkeypatch.setattr(marker_engine, "command_available", lambda *commands: r"E:\fake\marker_single.exe")
    monkeypatch.setattr(marker_engine, "run_command_template", fake_run_command_template)
    monkeypatch.setattr(marker_engine, "recommended_ocr_mode", lambda file_path: "ocr")

    sample = tmp_path / "scan.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MarkerEngine().run(sample, tmp_path / "marker")

    assert result.status == "success"
    assert "--disable_ocr" not in captured["template"]


def test_mineru_benchmark_defaults_to_ocr_method_for_latex(monkeypatch, tmp_path):
    captured = {}

    def fake_run_command_template(*, engine, template, file_path, output_dir, timeout=None):
        captured["template"] = template
        return "Cau 1: $x^2$", {"command": template}, 123

    monkeypatch.delenv("MINERU_CMD", raising=False)
    monkeypatch.delenv("MINERU_METHOD", raising=False)
    monkeypatch.setenv("MINERU_BACKEND", "pipeline")
    monkeypatch.setattr(mineru_engine, "command_available", lambda *commands: r"E:\fake\mineru.exe")
    monkeypatch.setattr(mineru_engine, "run_command_template", fake_run_command_template)

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MinerUEngine().run(sample, tmp_path / "mineru")

    assert result.status == "success"
    assert " -m ocr " in captured["template"]


def test_mineru_benchmark_uses_native_fast_path_for_text_pdf(monkeypatch, tmp_path):
    calls = []

    monkeypatch.delenv("MINERU_CMD", raising=False)
    monkeypatch.setenv("MINERU_BENCHMARK_ALLOW_NATIVE", "1")
    monkeypatch.setattr(mineru_engine, "recommended_ocr_mode", lambda file_path: "text")
    monkeypatch.setattr(
        mineru_engine,
        "extract_native_pdf_markdown",
        lambda file_path: {"text": "Cau 1 native", "method": "native-pdf-text", "page_count": 1, "image_map": {}},
    )
    monkeypatch.setattr(
        mineru_engine,
        "run_command_template",
        lambda **kwargs: calls.append(kwargs) or ("", {}, 0),
    )

    sample = tmp_path / "text.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MinerUEngine().run(sample, tmp_path / "mineru")

    assert result.status == "success"
    assert result.markdown == "Cau 1 native"
    assert result.raw["mode"] == "text"
    assert calls == []


def test_mineru_benchmark_auto_uses_ocr_for_detected_scan(monkeypatch, tmp_path):
    captured = {}

    def fake_run_command_template(*, engine, template, file_path, output_dir, timeout=None):
        captured["template"] = template
        return "Cau 1: $x^2$", {"command": template}, 123

    monkeypatch.delenv("MINERU_CMD", raising=False)
    monkeypatch.delenv("MINERU_METHOD", raising=False)
    monkeypatch.setenv("MINERU_BACKEND", "pipeline")
    monkeypatch.setattr(mineru_engine, "command_available", lambda *commands: r"E:\fake\mineru.exe")
    monkeypatch.setattr(mineru_engine, "run_command_template", fake_run_command_template)
    monkeypatch.setattr(mineru_engine, "recommended_ocr_mode", lambda file_path: "ocr")

    sample = tmp_path / "scan.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MinerUEngine().run(sample, tmp_path / "mineru")

    assert result.status == "success"
    assert " -m ocr " in captured["template"]


def test_mineru_benchmark_detect_env_uses_detector(monkeypatch, tmp_path):
    captured = {}

    def fake_run_command_template(*, engine, template, file_path, output_dir, timeout=None):
        captured["template"] = template
        return "Cau 1: $x^2$", {"command": template}, 123

    monkeypatch.delenv("MINERU_CMD", raising=False)
    monkeypatch.setenv("MINERU_METHOD", "detect")
    monkeypatch.setenv("MINERU_BACKEND", "pipeline")
    monkeypatch.setattr(mineru_engine, "command_available", lambda *commands: r"E:\fake\mineru.exe")
    monkeypatch.setattr(mineru_engine, "run_command_template", fake_run_command_template)
    monkeypatch.setattr(mineru_engine, "recommended_ocr_mode", lambda file_path: "ocr")

    sample = tmp_path / "scan.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MinerUEngine().run(sample, tmp_path / "mineru")

    assert result.status == "success"
    assert " -m ocr " in captured["template"]


def test_marker_benchmark_can_fallback_to_package_wrapper(monkeypatch, tmp_path):
    captured = {}

    def fake_run_command_template(*, engine, template, file_path, output_dir, timeout=None):
        captured["template"] = template
        return "Cau 1: $x^2$", {"command": template}, 123

    monkeypatch.delenv("MARKER_CMD", raising=False)
    monkeypatch.setattr(marker_engine, "package_available", lambda package: package == "marker")
    monkeypatch.setattr(marker_engine, "command_available", lambda *commands: None)
    monkeypatch.setattr(marker_engine, "run_command_template", fake_run_command_template)

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MarkerEngine().run(sample, tmp_path / "marker")

    assert result.status == "success"
    assert "app.benchmark.marker_subprocess" in captured["template"]


def test_marker_benchmark_uses_marker_specific_timeout(monkeypatch, tmp_path):
    captured = {}

    def fake_run_command_template(*, engine, template, file_path, output_dir, timeout=None):
        captured["timeout"] = timeout
        return "Cau 1: $x^2$", {"command": template}, 123

    monkeypatch.delenv("MARKER_CMD", raising=False)
    monkeypatch.delenv("MARKER_BENCHMARK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("OCR_BENCHMARK_PER_ENGINE_TIMEOUT", "600")
    monkeypatch.setenv("MARKER_BENCHMARK_PER_ENGINE_TIMEOUT", "1800")
    monkeypatch.setattr(marker_engine, "package_available", lambda package: package == "marker")
    monkeypatch.setattr(marker_engine, "command_available", lambda *commands: r"E:\fake\marker_single.exe")
    monkeypatch.setattr(marker_engine, "run_command_template", fake_run_command_template)

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MarkerEngine().run(sample, tmp_path / "marker")

    assert result.status == "success"
    assert captured["timeout"] == 1770


def test_marker_benchmark_can_run_without_timeout(monkeypatch, tmp_path):
    captured = {}

    def fake_run_command_template(*, engine, template, file_path, output_dir, timeout=None):
        captured["timeout"] = timeout
        return "Cau 1: $x^2$", {"command": template}, 123

    monkeypatch.delenv("MARKER_CMD", raising=False)
    monkeypatch.delenv("MARKER_BENCHMARK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("OCR_BENCHMARK_PER_ENGINE_TIMEOUT", "600")
    monkeypatch.setenv("MARKER_BENCHMARK_PER_ENGINE_TIMEOUT", "0")
    monkeypatch.setattr(marker_engine, "package_available", lambda package: package == "marker")
    monkeypatch.setattr(marker_engine, "command_available", lambda *commands: r"E:\fake\marker_single.exe")
    monkeypatch.setattr(marker_engine, "run_command_template", fake_run_command_template)

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MarkerEngine().run(sample, tmp_path / "marker")

    assert result.status == "success"
    assert captured["timeout"] is None
    assert base.benchmark_endpoint_timeout_seconds("marker") is None


def test_marker_benchmark_defaults_to_unlimited_even_with_global_timeout(monkeypatch, tmp_path):
    captured = {}

    def fake_run_command_template(*, engine, template, file_path, output_dir, timeout=None):
        captured["timeout"] = timeout
        return "Cau 1: $x^2$", {"command": template}, 123

    monkeypatch.delenv("MARKER_CMD", raising=False)
    monkeypatch.delenv("MARKER_BENCHMARK_PER_ENGINE_TIMEOUT", raising=False)
    monkeypatch.delenv("MARKER_BENCHMARK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("OCR_BENCHMARK_PER_ENGINE_TIMEOUT", "600")
    monkeypatch.setattr(marker_engine, "package_available", lambda package: package == "marker")
    monkeypatch.setattr(marker_engine, "command_available", lambda *commands: r"E:\fake\marker_single.exe")
    monkeypatch.setattr(marker_engine, "run_command_template", fake_run_command_template)

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.4 mocked")

    result = MarkerEngine().run(sample, tmp_path / "marker")

    assert result.status == "success"
    assert captured["timeout"] is None
    assert base.benchmark_endpoint_timeout_seconds("marker") is None


def test_endpoint_timeout_can_be_overridden_per_engine(monkeypatch):
    monkeypatch.setenv("OCR_BENCHMARK_PER_ENGINE_TIMEOUT", "600")
    monkeypatch.setenv("MARKER_BENCHMARK_PER_ENGINE_TIMEOUT", "1800")

    assert base.benchmark_endpoint_timeout_seconds("marker") == 1800
    assert base.benchmark_endpoint_timeout_seconds("mineru") == 600


def test_run_command_template_uses_capped_default_timeout(monkeypatch, tmp_path):
    captured = {}

    class FakeProcess:
        returncode = 0
        pid = 12345

        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            return "Cau 1: x = 1", ""

    monkeypatch.setenv("OCR_BENCHMARK_PER_ENGINE_TIMEOUT", "120")
    monkeypatch.delenv("OCR_BENCHMARK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    input_path = tmp_path / "input.pdf"
    input_path.write_bytes(b"%PDF-1.4 mocked")

    base.run_command_template(
        engine="fake",
        template='fake "{input}" "{output}"',
        file_path=input_path,
        output_dir=tmp_path / "out",
    )

    assert captured["timeout"] == 90


def test_run_command_template_preserves_explicit_unlimited_timeout(monkeypatch, tmp_path):
    captured = {}

    class FakeProcess:
        returncode = 0
        pid = 12345

        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            return "Cau 1: x = 1", ""

    monkeypatch.setenv("OCR_BENCHMARK_PER_ENGINE_TIMEOUT", "600")
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    input_path = tmp_path / "input.pdf"
    input_path.write_bytes(b"%PDF-1.4 mocked")

    base.run_command_template(
        engine="fake",
        template='fake "{input}" "{output}"',
        file_path=input_path,
        output_dir=tmp_path / "out",
        timeout=None,
    )

    assert captured["timeout"] is None


def test_run_command_template_kills_process_tree_on_timeout(monkeypatch, tmp_path):
    calls = []

    class FakeProcess:
        returncode = None
        pid = 54321

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
            self.returncode = -9
            return "", "timed out"

    def fake_run(args, **kwargs):
        calls.append(tuple(args))

        class TaskkillResult:
            returncode = 0
            stdout = ""
            stderr = ""

        return TaskkillResult()

    monkeypatch.setenv("OCR_BENCHMARK_PER_ENGINE_TIMEOUT", "60")
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(os, "name", "nt")

    input_path = tmp_path / "input.pdf"
    input_path.write_bytes(b"%PDF-1.4 mocked")

    try:
        base.run_command_template(
            engine="fake",
            template='fake "{input}" "{output}"',
            file_path=input_path,
            output_dir=tmp_path / "out",
        )
    except RuntimeError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("timeout should raise")

    assert ("taskkill", "/F", "/T", "/PID", "54321") in calls
