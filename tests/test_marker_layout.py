from types import SimpleNamespace

from app.services.document_blocks import build_document_pages
from app.services.marker_ocr import _flatten_marker_json_layout


def _node(block_type: str, bbox, html: str = "", children=None, block_id: str = ""):
    return SimpleNamespace(
        block_type=block_type,
        bbox=bbox,
        html=html,
        children=children or [],
        id=block_id,
    )


def test_flatten_marker_json_layout_normalizes_blocks_and_figures():
    rendered = SimpleNamespace(
        children=[
            _node(
                "Page",
                [0, 0, 200, 400],
                children=[
                    _node("SectionHeader", [20, 20, 180, 50], "<h2>Câu 1</h2>", block_id="/page/0/SectionHeader/1"),
                    _node("Equation", [20, 60, 180, 100], "<math>x^2 + 1 = 0</math>", block_id="/page/0/Equation/2"),
                    _node("Figure", [25, 120, 150, 220], "", block_id="/page/0/Figure/3"),
                ],
            )
        ]
    )

    blocks, figures = _flatten_marker_json_layout(rendered)

    assert [block["kind"] for block in blocks] == ["heading", "formula", "figure"]
    assert blocks[0]["page_num"] == 1
    assert blocks[0]["bbox"] == [0.1, 0.05, 0.9, 0.125]
    assert blocks[1]["text"] == "$x^2 + 1 = 0$"
    assert figures == [
        {
            "figure_id": "p1_marker_fig_1",
            "page_num": 1,
            "bbox": [0.125, 0.3, 0.75, 0.55],
            "placeholder": "_page_0_Figure_3.jpeg",
            "source": "marker_json",
            "marker_block_id": "/page/0/Figure/3",
        }
    ]


def test_marker_blocks_feed_segmentation_with_true_bbox():
    ocr_result = {
        "method": "marker",
        "blocks": [
            {
                "block_id": "p1_marker_1",
                "page_num": 1,
                "order": 0,
                "text": "Câu 1. Tính $A$",
                "kind": "heading",
                "bbox": [0.1, 0.1, 0.8, 0.2],
            }
        ],
    }

    pages = build_document_pages(ocr_result)

    assert len(pages) == 1
    assert pages[0].blocks[0].source == "marker"
    assert pages[0].blocks[0].bbox == (0.1, 0.1, 0.8, 0.2)
