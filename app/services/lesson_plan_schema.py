"""
KHBD (Kế hoạch bài dạy) — Công văn 5512 schema, hằng số chương trình & validator.

- `KHBD_SCHEMA`         : schema Gemini `response_schema` (ánh xạ 1-1 với Phụ lục IV).
- Hằng số năng lực/phẩm chất GDPT 2018 để ràng buộc prompt.
- `validate_khbd()`     : gác cổng cấu trúc + grounding + chống sáo rỗng.

Tham chiếu cấu trúc CV5512 (đã xác minh): I. Mục tiêu (kiến thức / năng lực
[chung + đặc thù] / phẩm chất) → II. Thiết bị dạy học và học liệu → III. Tiến
trình dạy học (4 hoạt động: Mở đầu / Hình thành kiến thức / Luyện tập / Vận dụng;
mỗi hoạt động: a) Mục tiêu b) Nội dung c) Sản phẩm d) Tổ chức thực hiện [4 bước]).
"""

from __future__ import annotations
import re
from typing import Any, Dict, List

# ── Hằng số chương trình GDPT 2018 ──────────────────────────────────────────

# 3 năng lực chung (mọi môn)
NANG_LUC_CHUNG = [
    "Tự chủ và tự học",
    "Giao tiếp và hợp tác",
    "Giải quyết vấn đề và sáng tạo",
]

# 5 thành tố năng lực đặc thù môn Toán
NANG_LUC_DAC_THU_TOAN = [
    "Tư duy và lập luận toán học",
    "Mô hình hóa toán học",
    "Giải quyết vấn đề toán học",
    "Giao tiếp toán học",
    "Sử dụng công cụ, phương tiện học toán",
]

# 5 phẩm chất chủ yếu
PHAM_CHAT = ["Yêu nước", "Nhân ái", "Chăm chỉ", "Trung thực", "Trách nhiệm"]

# 4 loại hoạt động trong tiến trình dạy học (III)
HOAT_DONG_TYPES = ["mo_dau", "hinh_thanh_kien_thuc", "luyen_tap", "van_dung"]
HOAT_DONG_LABELS = {
    "mo_dau": "Hoạt động 1: Mở đầu (Xác định vấn đề)",
    "hinh_thanh_kien_thuc": "Hoạt động 2: Hình thành kiến thức mới",
    "luyen_tap": "Hoạt động 3: Luyện tập",
    "van_dung": "Hoạt động 4: Vận dụng",
}

# Năng lực đặc thù theo môn (mở rộng sau khi thêm môn ngoài Toán)
NANG_LUC_DAC_THU_BY_SUBJECT = {"toan": NANG_LUC_DAC_THU_TOAN}


# ── Gemini response_schema (ánh xạ Phụ lục IV) ──────────────────────────────

_NANG_LUC_ITEM = {
    "type": "OBJECT",
    "properties": {
        "ten":      {"type": "STRING"},   # tên năng lực (chọn từ danh mục)
        "bieu_hien": {"type": "STRING"},  # biểu hiện CỤ THỂ gắn nội dung bài
    },
    "required": ["ten", "bieu_hien"],
}

_PHAM_CHAT_ITEM = {
    "type": "OBJECT",
    "properties": {
        "ten":       {"type": "STRING"},
        "bieu_hien": {"type": "STRING"},
    },
    "required": ["ten", "bieu_hien"],
}

_HOAT_DONG_ITEM = {
    "type": "OBJECT",
    "properties": {
        "loai":            {"type": "STRING"},  # one of HOAT_DONG_TYPES
        "ten_hoat_dong":   {"type": "STRING"},
        "thoi_gian_phut":  {"type": "INTEGER"},
        "muc_tieu":        {"type": "STRING"},
        "noi_dung": {
            "type": "OBJECT",
            "properties": {
                "mo_ta":            {"type": "STRING"},
                "cau_hoi_nhiem_vu": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["mo_ta", "cau_hoi_nhiem_vu"],
        },
        "san_pham": {
            "type": "OBJECT",
            "properties": {
                "mo_ta":           {"type": "STRING"},
                "ket_qua_mong_doi": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["mo_ta", "ket_qua_mong_doi"],
        },
        "to_chuc_thuc_hien": {
            "type": "OBJECT",
            "properties": {
                "chuyen_giao_nhiem_vu": {"type": "STRING"},
                "thuc_hien_nhiem_vu":   {"type": "STRING"},
                "bao_cao_thao_luan":    {"type": "STRING"},
                "ket_luan_nhan_dinh":   {"type": "STRING"},
            },
            "required": [
                "chuyen_giao_nhiem_vu", "thuc_hien_nhiem_vu",
                "bao_cao_thao_luan", "ket_luan_nhan_dinh",
            ],
        },
    },
    "required": [
        "loai", "ten_hoat_dong", "muc_tieu",
        "noi_dung", "san_pham", "to_chuc_thuc_hien",
    ],
}

KHBD_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "muc_tieu": {
            "type": "OBJECT",
            "properties": {
                "kien_thuc": {"type": "ARRAY", "items": {"type": "STRING"}},
                "nang_luc": {
                    "type": "OBJECT",
                    "properties": {
                        "chung":   {"type": "ARRAY", "items": _NANG_LUC_ITEM},
                        "dac_thu": {"type": "ARRAY", "items": _NANG_LUC_ITEM},
                    },
                    "required": ["chung", "dac_thu"],
                },
                "pham_chat": {"type": "ARRAY", "items": _PHAM_CHAT_ITEM},
                # Mã YCCĐ đã neo (phải nằm trong danh mục cung cấp) — truy vết grounding
                "yccd_refs": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["kien_thuc", "nang_luc", "pham_chat", "yccd_refs"],
        },
        "thiet_bi_day_hoc": {"type": "ARRAY", "items": {"type": "STRING"}},
        "tien_trinh": {"type": "ARRAY", "items": _HOAT_DONG_ITEM},
    },
    "required": ["muc_tieu", "thiet_bi_day_hoc", "tien_trinh"],
}


# ── Validator ───────────────────────────────────────────────────────────────

# Heuristic chống sáo rỗng cho môn Toán: nội dung/sản phẩm phải "cụ thể" —
# chứa chữ số hoặc ký hiệu toán (biểu thức), không chỉ mô tả lý thuyết chung.
_RE_SPECIFIC_MATH = re.compile(r"[0-9]|\$|\\frac|\\sqrt|[=+\-×÷/^<>%]")


def _is_specific(text: str, subject_code: str) -> bool:
    """True nếu chuỗi đủ 'cụ thể'. Với Toán dùng heuristic ký hiệu/số."""
    if not text or not text.strip():
        return False
    if subject_code == "toan":
        return bool(_RE_SPECIFIC_MATH.search(text))
    # Môn khác: chỉ cần không rỗng (heuristic chặt hơn sẽ thêm sau)
    return len(text.strip()) >= 10


def validate_khbd(
    khbd: Dict[str, Any],
    valid_yccd_codes: List[str],
    subject_code: str = "toan",
) -> Dict[str, Any]:
    """Gác cổng KHBD: cấu trúc + grounding + chống sáo rỗng.

    Args:
        khbd: dict KHBD (đã parse từ JSON model trả).
        valid_yccd_codes: danh mục mã YCCĐ hợp lệ cho bài (từ curriculum_yccd join).
        subject_code: dùng cho heuristic chống sáo rỗng.

    Returns:
        {"ok": bool, "errors": [...], "warnings": [...]}
        - errors  → từ chối, cần retry / sửa.
        - warnings → cho qua nhưng đánh dấu để giáo viên xem.
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(khbd, dict):
        return {"ok": False, "errors": ["KHBD không phải object JSON."], "warnings": []}

    # ── I. Mục tiêu ──
    mt = khbd.get("muc_tieu")
    if not isinstance(mt, dict):
        errors.append("Thiếu mục 'muc_tieu'.")
        mt = {}

    kien_thuc = mt.get("kien_thuc") or []
    if not kien_thuc:
        errors.append("Mục tiêu kiến thức rỗng.")

    # Grounding: yccd_refs phải có và nằm trong danh mục hợp lệ
    refs = mt.get("yccd_refs") or []
    valid_set = set(valid_yccd_codes)
    if not refs:
        errors.append("Thiếu 'yccd_refs' — KHBD không neo vào yêu cầu cần đạt nào.")
    else:
        unknown = [r for r in refs if r not in valid_set]
        if unknown:
            errors.append(f"yccd_refs chứa mã ngoài danh mục bài: {unknown}.")
    if valid_set and refs and not (set(refs) & valid_set):
        errors.append("Không có yccd_ref nào khớp yêu cầu cần đạt của bài.")

    nang_luc = mt.get("nang_luc") or {}
    if not (nang_luc.get("chung") or nang_luc.get("dac_thu")):
        warnings.append("Mục tiêu năng lực rỗng.")
    if not (mt.get("pham_chat")):
        warnings.append("Mục tiêu phẩm chất rỗng.")

    # ── III. Tiến trình dạy học ──
    tt = khbd.get("tien_trinh") or []
    if not tt:
        errors.append("Thiếu 'tien_trinh' (các hoạt động dạy học).")

    seen_types = set()
    for i, hd in enumerate(tt, 1):
        if not isinstance(hd, dict):
            errors.append(f"Hoạt động {i} không hợp lệ.")
            continue
        loai = hd.get("loai")
        if loai in HOAT_DONG_TYPES:
            seen_types.add(loai)

        nd = hd.get("noi_dung") or {}
        sp = hd.get("san_pham") or {}
        cau_hoi = nd.get("cau_hoi_nhiem_vu") or []
        ket_qua = sp.get("ket_qua_mong_doi") or []

        if not cau_hoi:
            errors.append(f"Hoạt động {i} ({loai}): 'cau_hoi_nhiem_vu' rỗng.")
        if not ket_qua:
            errors.append(f"Hoạt động {i} ({loai}): 'ket_qua_mong_doi' rỗng.")

        # Chống sáo rỗng: ít nhất 1 câu hỏi/nhiệm vụ phải 'cụ thể'
        if cau_hoi and not any(_is_specific(c, subject_code) for c in cau_hoi):
            warnings.append(
                f"Hoạt động {i} ({loai}): câu hỏi/nhiệm vụ có vẻ chung chung "
                f"(không chứa số/biểu thức cụ thể)."
            )

        toc = hd.get("to_chuc_thuc_hien") or {}
        missing_steps = [
            s for s in ("chuyen_giao_nhiem_vu", "thuc_hien_nhiem_vu",
                        "bao_cao_thao_luan", "ket_luan_nhan_dinh")
            if not (toc.get(s) or "").strip()
        ]
        if missing_steps:
            warnings.append(f"Hoạt động {i} ({loai}): thiếu bước {missing_steps}.")

    # Bắt buộc tối thiểu 2 loại hoạt động (Mở đầu + Hình thành KT); khuyến nghị đủ 4
    if "mo_dau" not in seen_types:
        warnings.append("Thiếu hoạt động Mở đầu.")
    if "hinh_thanh_kien_thuc" not in seen_types:
        errors.append("Thiếu hoạt động Hình thành kiến thức mới.")
    missing4 = [t for t in HOAT_DONG_TYPES if t not in seen_types]
    if missing4:
        warnings.append(f"Chưa đủ 4 loại hoạt động — thiếu: {missing4}.")

    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings}
