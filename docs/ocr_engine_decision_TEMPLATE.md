# OCR Engine Decision — Template

> Document này được fill sau khi có kết quả benchmark. Mỗi tracking decision dùng template này.
>
> **Status**: TEMPLATE — chưa có data thực.

## Context

- **Date**: YYYY-MM-DD
- **Batch ID(s)**: `<batch_id>`
- **Dataset**: N files (vn_scanned: X, stem_text_layer: X, stem_scanned: X, layout_heavy: X, text_heavy: X, edge_case: X)
- **Engines tested**: marker, mineru, paddle (PaddleOCR-VL), granite-docling, dots, [+olmocr nếu có GPU đủ]
- **Variants per cell**: 3 (median)
- **Hardware**: GTX 1650 4GB (CPU mode fallback cho most engines)
- **Total cells**: N_files × N_engines = NN; total runs = NN × variants

## Goal

Chọn (hoặc xác nhận) stack OCR cho production parser pipeline, đáp ứng:
1. **Tốc độ**: < 60s cho 5-10 trang
2. **Accuracy**: VN diacritic tốt, LaTeX rendering chính xác, image extraction đủ cho overlay
3. **License**: ưu tiên Apache 2.0 / MIT, chấp nhận AGPL có điều kiện
4. **Cost**: rẻ nhất có thể, GPU tier nhỏ nếu được

## Raw results

Paste/embed:
- `benchmark_results_batch/<batch_id>/matrix.md` — score grid
- `benchmark_results_batch/<batch_id>/summary.md` — aggregate ranking

## Analysis

### Per-engine performance (median across all files)

| Engine | Avg Score | Avg Latency | LaTeX valid | VN dia | Image | Notes |
|---|---:|---:|---:|---:|---:|---|
| marker | N/A | N/A ms | N/A | N/A | N/A | Current production |
| mineru | N/A | N/A ms | N/A | N/A | N/A | |
| paddle (VL) | N/A | N/A ms | N/A | N/A | N/A | Apache 2.0 |
| granite-docling | N/A | N/A ms | N/A | N/A | N/A | Compact 258M |
| dots | N/A | N/A ms | N/A | N/A | N/A | MIT, multilingual |
| olmocr | N/A | N/A ms | N/A | N/A | N/A | |

### Per-category winner

| Category | Winner | Runner-up | Gap |
|---|---|---|---|
| vn_scanned | TBD | TBD | TBD |
| stem_text_layer | TBD | TBD | TBD |
| stem_scanned | TBD | TBD | TBD |
| layout_heavy | TBD | TBD | TBD |
| text_heavy | TBD | TBD | TBD |
| edge_case | TBD | TBD | TBD |

### Observations

- [ ] VN diacritic: which engines passed >0.9? Failed <0.5?
- [ ] LaTeX complexity: which engine handled `\frac`, `\int`, `\begin{matrix}`?
- [ ] Image extraction: which engine output `![](file)` reliable vs base64?
- [ ] Speed cliff: which engine consistently > 60s/file?
- [ ] Crashes / timeouts: pattern theo file category nào?

## Recommended tracks

### Track A — Conservative (additive, low risk)

**Action**: Giữ Marker primary trong [pipeline.py](math-parser-mvp/app/services/pipeline.py). Thêm `paddle` (PaddleOCR-VL) như fallback cho VN-scanned files (subject `vn_scanned`).

**Files to change**:
- [ocr_router.py](math-parser-mvp/app/services/ocr_router.py) — thêm logic: if `subject in STEM_SUBJECTS and detected_vn_scanned`, route Paddle-VL trước Marker
- [pipeline.py](math-parser-mvp/app/services/pipeline.py) — extend cascade: Marker → PaddleVL → Docling → Pix2Text

**Tradeoff**: +1 engine load (model RAM), +setup deps. Migration cost low.

**Khi nào pick**: Marker vẫn win majority, chỉ thua trên vn_scanned.

---

### Track B — Aggressive (cutover)

**Action**: Replace Marker bằng PaddleOCR-VL (hoặc MinerU 2.5 VLM nếu accept AGPL) làm primary.

**Files to change**:
- [pipeline.py](math-parser-mvp/app/services/pipeline.py) — đổi `_ocr_marker` → `_ocr_paddle_vl`
- [local_ocr_service.py](math-parser-mvp/app/services/local_ocr_service.py) — bump cache schema → `v11_paddle_vl`, update escalation logic
- [ocr_router.py](math-parser-mvp/app/services/ocr_router.py) — đổi `MARKER` default → `PADDLE_VL`
- Tests: update tests cho `marker_engine` → `paddle_vl_engine`

**Tradeoff**: ~500-800 LOC change. Migration risk medium. Cần A/B test 1-2 tuần.

**Khi nào pick**: PaddleOCR-VL/MinerU 2.5 win ≥3 categories và not worse hơn Marker ≥10% trong các category còn lại.

---

### Track C — Specialized (subject-routed)

**Action**: Multiple engine optimal mỗi subject. Update `SUBJECT_OCR_MAP` để route theo data-driven.

**Files to change**:
- [ocr_router.py:SUBJECT_OCR_MAP](math-parser-mvp/app/services/ocr_router.py) — extend với engine override
- [pipeline.py](math-parser-mvp/app/services/pipeline.py) — dispatch theo full backend list

**Tradeoff**: Codebase complexity cao nhất. Cần regression test mỗi subject. Bù lại optimal accuracy.

**Khi nào pick**: Không có engine nào win all categories, và sự khác biệt giữa engines >15 score.

---

## Decision

**Selected track**: TBD (A / B / C)

**Rationale**:

**Effort estimate**:
- Implementation: TBD days
- Testing + A/B: TBD days
- Rollout: TBD week

**Rollback plan**:

**Owner**: TBD

**Date**: YYYY-MM-DD

## Follow-up actions

- [ ] Implement chosen track
- [ ] Add tests cover new engine in production pipeline
- [ ] Update [memory/parser_pipeline_architecture.md](memory/parser_pipeline_architecture.md)
- [ ] Bump `OCR_CACHE_SCHEMA_VERSION` để invalidate cache cũ
- [ ] Monitor production quality_report trong 1-2 tuần đầu sau cutover
- [ ] Cleanup unused engine adapters (nếu Track B)
