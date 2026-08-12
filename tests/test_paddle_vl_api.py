"""Remote PaddleOCR-VL API client (app/services/paddle_vl_api.py).

Mocks the sync HTTP steps (submit/poll/fetch) so the orchestration + JSONL
parsing logic is exercised without a live server. Verifies the response is
converted into the production OCR shape with normalized-bbox review blocks.
"""
import json

import pytest

from app.services import paddle_vl_api as api


def _configure(monkeypatch):
    monkeypatch.setattr(api, "_API_URL", "https://example.test/api/v2/ocr/jobs")
    monkeypatch.setattr(api, "_API_KEY", "test-token")
    monkeypatch.setattr(api, "_POLL_INTERVAL", 0)  # no real sleeping


_JSONL_ONE_PAGE = json.dumps({
    "result": {
        "layoutParsingResults": [
            {
                "prunedResult": {
                    "width": 1000,
                    "height": 2000,
                    "parsing_res_list": [
                        {"block_label": "header", "block_content": "Trường THCS", "block_bbox": [100, 200, 500, 300]},
                        {"block_label": "formula", "block_content": "$x^2$", "block_bbox": [100, 400, 400, 500]},
                    ],
                },
                "markdown": {"text": "# Đề thi\n$x^2$", "images": {}},
            }
        ]
    }
})


def _patch_flow(monkeypatch, *, jsonl: str, state: str = "done", error: str | None = None):
    monkeypatch.setattr(api, "_submit_job", lambda fp: "job123")

    def fake_poll(job_id):
        d = {"state": state}
        if state == "done":
            d["resultUrl"] = {"jsonUrl": "https://example.test/result.jsonl"}
        if error:
            d["errorMsg"] = error
        return d

    monkeypatch.setattr(api, "_poll_job", fake_poll)
    monkeypatch.setattr(api, "_fetch_text", lambda url: jsonl)


@pytest.mark.asyncio
async def test_api_parses_pruned_result_to_blocks(monkeypatch):
    _configure(monkeypatch)
    _patch_flow(monkeypatch, jsonl=_JSONL_ONE_PAGE)

    out = await api.run_paddle_vl_api("exam.pdf", "hash123")

    assert "Đề thi" in out["text"] and "$x^2$" in out["text"]
    assert out["page_count"] == 1
    blocks = out["blocks"]
    assert [b["kind"] for b in blocks] == ["text", "equation"]   # header→text, formula→equation
    assert all(b["page_num"] == 1 and b["source"] == "paddle-vl" for b in blocks)
    # bbox normalized by width/height → [0,1]
    assert blocks[0]["bbox"] == [0.1, 0.1, 0.5, 0.15]
    assert blocks[1]["bbox"] == [0.1, 0.2, 0.4, 0.25]


@pytest.mark.asyncio
async def test_api_missing_pruned_result_blocks_empty(monkeypatch):
    _configure(monkeypatch)
    jsonl = json.dumps({"result": {"layoutParsingResults": [{"markdown": {"text": "chỉ có text", "images": {}}}]}})
    _patch_flow(monkeypatch, jsonl=jsonl)

    out = await api.run_paddle_vl_api("exam.pdf", "h")
    assert out["text"] == "chỉ có text"
    assert out["blocks"] == []
    assert any("no_layout_blocks" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_api_multi_line_jsonl_page_numbering(monkeypatch):
    _configure(monkeypatch)
    # API batches pages across lines — page numbers must keep incrementing.
    line1 = json.dumps({"result": {"layoutParsingResults": [
        {"prunedResult": {"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "text", "block_content": "p1", "block_bbox": [10, 10, 90, 50]}]},
         "markdown": {"text": "page1", "images": {}}},
    ]}})
    line2 = json.dumps({"result": {"layoutParsingResults": [
        {"prunedResult": {"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "table", "block_content": "p2", "block_bbox": [10, 10, 90, 50]}]},
         "markdown": {"text": "page2", "images": {}}},
    ]}})
    _patch_flow(monkeypatch, jsonl=line1 + "\n" + line2)

    out = await api.run_paddle_vl_api("exam.pdf", "h")
    assert out["page_count"] == 2
    assert [b["page_num"] for b in out["blocks"]] == [1, 2]
    assert "page1" in out["text"] and "page2" in out["text"]


@pytest.mark.asyncio
async def test_api_failed_state_no_raise(monkeypatch):
    _configure(monkeypatch)
    _patch_flow(monkeypatch, jsonl="", state="failed", error="bad file")

    out = await api.run_paddle_vl_api("exam.pdf", "h")
    assert out["text"] == "" and out["blocks"] == []
    assert any("paddle_vl_api_failed" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_api_unconfigured(monkeypatch):
    monkeypatch.setattr(api, "_API_URL", "")
    monkeypatch.setattr(api, "_API_KEY", "")
    out = await api.run_paddle_vl_api("exam.pdf", "h")
    assert any("unconfigured" in w for w in out["warnings"])
    assert out["blocks"] == []


def test_is_api_configured(monkeypatch):
    monkeypatch.setattr(api, "_API_URL", "https://x")
    monkeypatch.setattr(api, "_API_KEY", "k")
    assert api.is_api_configured() is True
    monkeypatch.setattr(api, "_API_KEY", "")
    assert api.is_api_configured() is False
