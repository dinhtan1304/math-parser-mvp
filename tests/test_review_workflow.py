from pathlib import Path

from app.services.asset_storage import LocalAssetStorage
from app.services.review_workflow import assign_figures_to_drafts


def test_local_asset_storage_deduplicates_by_hash(tmp_path: Path):
    storage = LocalAssetStorage(root=tmp_path)

    key1, digest1 = storage.put_bytes(b"same-content", suffix=".png", namespace="figures")
    key2, digest2 = storage.put_bytes(b"same-content", suffix=".png", namespace="figures")

    assert key1 == key2
    assert digest1 == digest2
    assert (tmp_path / key1).exists()
    assert storage.public_url(key1).endswith(key1)


def test_assign_figures_prefers_clear_nearest_question():
    drafts = [
        {"draft_id": 1, "page_num": 1, "bbox": [0.05, 0.05, 0.45, 0.35]},
        {"draft_id": 2, "page_num": 1, "bbox": [0.05, 0.55, 0.45, 0.85]},
    ]
    figures = [
        {"asset_id": 10, "page_num": 1, "bbox": [0.48, 0.12, 0.70, 0.28]},
    ]

    assigned = assign_figures_to_drafts(drafts, figures)

    assert assigned[1] == [10]
    assert assigned[2] == []


def test_assign_figures_leaves_ambiguous_candidate_unassigned():
    drafts = [
        {"draft_id": 1, "page_num": 1, "bbox": [0.05, 0.10, 0.40, 0.35]},
        {"draft_id": 2, "page_num": 1, "bbox": [0.45, 0.10, 0.80, 0.35]},
    ]
    figures = [
        {"asset_id": 20, "page_num": 1, "bbox": [0.39, 0.15, 0.46, 0.30]},
    ]

    assigned = assign_figures_to_drafts(drafts, figures)

    assert assigned[1] == []
    assert assigned[2] == []
