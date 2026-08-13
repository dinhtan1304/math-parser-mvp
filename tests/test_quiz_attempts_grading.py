"""
Test luồng làm bài + chấm bài — 10 endpoint, TRƯỚC ĐÂY 0 test hành vi.

TẠI SAO VÙNG NÀY ĐÁNH DẤU RỦI RO
Bản kiểm kê 13/08 xếp "chấm tay" vào nhóm rủi ro: có giao diện
(/quizzes/[id]/grade), có 10 endpoint, nhưng không một bài test nào chạy qua
luồng thật. Đây cũng là nơi tiền và niềm tin gặp nhau — chấm sai điểm là hỏng
quan hệ giáo viên–học sinh, còn rò rỉ đáp án là hỏng cả đề.

HAI CHẾ ĐỘ CHẤM (theo quiz.settings.grading_mode)
  auto   (mặc định) — chấm ngay khi nộp, attempt → completed
  manual            — attempt → pending_review, giáo viên chấm từng câu rồi chốt

Luồng làm bài đang sống thực tế là luồng IELTS qua link công khai (khách vô
danh, student_id=NULL). Test ở đây phủ cả hai: khách và người đã đăng nhập.
"""

API = "/api/v1"


# ─────────────────────────────────────────────────────────────
# Trợ giúp
# ─────────────────────────────────────────────────────────────

def _de_da_xuat_ban(client, headers, *, grading_mode="auto", cau=None):
    """Tạo đề đã xuất bản kèm câu hỏi. Trả (quiz, danh sách câu)."""
    quiz = client.post(f"{API}/quizzes", json={
        "name": "Đề kiểm tra", "settings": {"grading_mode": grading_mode},
    }, headers=headers).json()

    cau = cau or [{
        "type": "multiple_choice",
        "question_text": "2 + 2 = ?",
        "choices": [
            {"key": "A", "text": "3", "is_correct": False},
            {"key": "B", "text": "4", "is_correct": True},
        ],
        "answer": "B",
        "points": 2,
    }]
    ds = []
    for c in cau:
        r = client.post(f"{API}/quizzes/{quiz['id']}/questions", json=c, headers=headers)
        assert r.status_code == 201, r.text
        ds.append(r.json())

    r = client.patch(f"{API}/quizzes/{quiz['id']}", json={"status": "published"}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json(), ds


def _cau_tu_luan():
    return {
        "type": "essay",
        "question_text": "Trình bày cách giải phương trình bậc hai.",
        "points": 5,
        "has_correct_answer": False,
    }


# ─────────────────────────────────────────────────────────────
# Chấm tự động
# ─────────────────────────────────────────────────────────────

def test_lam_bai_va_cham_tu_dong_dung_diem(client, make_teacher):
    _, h = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, h)

    att = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]}, headers=h)
    assert att.status_code == 201, att.text
    aid = att.json()["id"]
    assert att.json()["status"] == "in_progress"

    nop = client.post(f"{API}/quiz-attempts/{aid}/submit", json={
        "answers": [{"question_id": cau[0]["id"], "given_answer": "B"}],
    }, headers=h)
    assert nop.status_code == 200, nop.text
    d = nop.json()

    assert d["status"] == "completed"
    assert float(d["score"]) == 2.0, f"trả lời đúng mà không được đủ điểm: {d}"
    assert float(d["max_score"]) == 2.0
    assert float(d["percentage"]) == 100.0
    assert d["correct_count"] == 1


def test_tra_loi_sai_thi_khong_duoc_diem(client, make_teacher):
    _, h = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, h)

    aid = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]},
                      headers=h).json()["id"]
    d = client.post(f"{API}/quiz-attempts/{aid}/submit", json={
        "answers": [{"question_id": cau[0]["id"], "given_answer": "A"}],
    }, headers=h).json()

    assert float(d["score"]) == 0.0
    assert d["correct_count"] == 0
    assert float(d["percentage"]) == 0.0


def test_khong_tra_loi_thi_van_tinh_tren_tong_diem_de(client, make_teacher):
    """max_score tính trên TOÀN BỘ câu của đề, không phải số câu đã trả lời.

    Nếu tính sai, bỏ trắng hết vẫn ra 100% — lỗi rất khó phát hiện bằng mắt.
    """
    _, h = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, h)

    aid = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]},
                      headers=h).json()["id"]
    d = client.post(f"{API}/quiz-attempts/{aid}/submit", json={"answers": []}, headers=h).json()

    assert float(d["max_score"]) == 2.0, "max_score phải theo tổng điểm đề"
    assert float(d["score"]) == 0.0
    assert float(d["percentage"]) == 0.0


def test_khach_vo_danh_lam_bai_duoc(client, make_teacher):
    """Luồng đang sống thật: vào bằng link, không đăng nhập."""
    _, h = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, h)

    att = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]})
    assert att.status_code == 201, att.text
    assert att.json()["student_id"] is None, "khách phải có student_id rỗng"

    aid = att.json()["id"]
    d = client.post(f"{API}/quiz-attempts/{aid}/submit", json={
        "answers": [{"question_id": cau[0]["id"], "given_answer": "B"}],
    }).json()
    assert d["status"] == "completed"
    assert float(d["score"]) == 2.0


def test_khong_lam_duoc_de_chua_xuat_ban(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    quiz = client.post(f"{API}/quizzes", json={"name": "Đề nháp"}, headers=hA).json()

    r = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]}, headers=hB)
    assert r.status_code == 403, f"đề nháp của A cho B làm (mã {r.status_code})"


def test_bat_dau_lam_de_khong_ton_tai(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": 999999}, headers=h)
    assert r.status_code == 404


# ─────────────────────────────────────────────────────────────
# Chấm tay — luồng bị đánh dấu rủi ro
# ─────────────────────────────────────────────────────────────

def test_de_che_do_cham_tay_vao_hang_cho_duyet(client, make_teacher):
    _, h = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, h, grading_mode="manual", cau=[_cau_tu_luan()])

    aid = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]},
                      headers=h).json()["id"]
    d = client.post(f"{API}/quiz-attempts/{aid}/submit", json={
        "answers": [{"question_id": cau[0]["id"], "given_answer": "Dùng công thức nghiệm."}],
    }, headers=h).json()

    assert d["status"] == "pending_review", f"chế độ manual mà chấm luôn: {d}"
    assert d["score"] is None, "chưa chấm mà đã có điểm"
    assert float(d["max_score"]) == 5.0


def test_hang_cho_duyet_hien_dung_bai(client, make_teacher):
    _, h = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, h, grading_mode="manual", cau=[_cau_tu_luan()])

    aid = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]},
                      headers=h).json()["id"]
    client.post(f"{API}/quiz-attempts/{aid}/submit", json={
        "answers": [{"question_id": cau[0]["id"], "given_answer": "Bài làm"}],
    }, headers=h)

    ds = client.get(f"{API}/quiz-attempts/quiz/{quiz['id']}/pending-review", headers=h)
    assert ds.status_code == 200, ds.text
    assert aid in {a["id"] for a in ds.json()}


def test_cham_tung_cau_roi_chot_diem(client, make_teacher):
    """Luồng chấm tay đầy đủ — đây là thứ trang /quizzes/[id]/grade làm."""
    _, h = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, h, grading_mode="manual", cau=[_cau_tu_luan()])

    aid = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]},
                      headers=h).json()["id"]
    nop = client.post(f"{API}/quiz-attempts/{aid}/submit", json={
        "answers": [{"question_id": cau[0]["id"], "given_answer": "Bài làm của học sinh"}],
    }, headers=h).json()

    answer_id = nop["answers"][0]["id"]

    cham = client.patch(
        f"{API}/quiz-attempts/{aid}/answers/{answer_id}/grade",
        json={"points_earned": 4, "is_correct": True, "teacher_comment": "Trình bày tốt"},
        headers=h,
    )
    assert cham.status_code == 200, cham.text
    assert float(cham.json()["points_earned"]) == 4.0
    assert cham.json()["teacher_comment"] == "Trình bày tốt"

    chot = client.post(f"{API}/quiz-attempts/{aid}/finalize-grading", json={}, headers=h)
    assert chot.status_code == 200, chot.text
    d = chot.json()
    assert d["status"] == "completed", "chốt xong mà vẫn treo chờ duyệt"
    assert float(d["score"]) == 4.0, f"điểm chốt không khớp điểm đã chấm: {d}"
    assert d["graded_at"] is not None
    assert d["graded_by_id"] is not None


def test_chi_chu_de_moi_duoc_cham(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, hA, grading_mode="manual", cau=[_cau_tu_luan()])

    aid = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]},
                      headers=hA).json()["id"]
    nop = client.post(f"{API}/quiz-attempts/{aid}/submit", json={
        "answers": [{"question_id": cau[0]["id"], "given_answer": "x"}],
    }, headers=hA).json()
    answer_id = nop["answers"][0]["id"]

    r = client.patch(f"{API}/quiz-attempts/{aid}/answers/{answer_id}/grade",
                     json={"points_earned": 5}, headers=hB)
    assert r.status_code == 403, f"B chấm được bài trong đề của A (mã {r.status_code})"


def test_khong_xem_duoc_hang_cho_duyet_cua_de_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    quiz, _ = _de_da_xuat_ban(client, hA, grading_mode="manual", cau=[_cau_tu_luan()])

    r = client.get(f"{API}/quiz-attempts/quiz/{quiz['id']}/pending-review", headers=hB)
    assert r.status_code in (403, 404)


def test_khong_cham_duoc_bai_da_cham_xong(client, make_teacher):
    """Chấm lại bài đã chốt phải bị chặn, nếu không điểm sẽ trôi."""
    _, h = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, h, grading_mode="manual", cau=[_cau_tu_luan()])

    aid = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]},
                      headers=h).json()["id"]
    nop = client.post(f"{API}/quiz-attempts/{aid}/submit", json={
        "answers": [{"question_id": cau[0]["id"], "given_answer": "x"}],
    }, headers=h).json()
    answer_id = nop["answers"][0]["id"]

    client.patch(f"{API}/quiz-attempts/{aid}/answers/{answer_id}/grade",
                 json={"points_earned": 3}, headers=h)
    client.post(f"{API}/quiz-attempts/{aid}/finalize-grading", json={}, headers=h)

    lai = client.patch(f"{API}/quiz-attempts/{aid}/answers/{answer_id}/grade",
                       json={"points_earned": 5}, headers=h)
    assert lai.status_code == 400, f"chấm lại được bài đã chốt (mã {lai.status_code})"


def test_diem_cham_khong_duoc_am(client, make_teacher):
    _, h = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, h, grading_mode="manual", cau=[_cau_tu_luan()])

    aid = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]},
                      headers=h).json()["id"]
    nop = client.post(f"{API}/quiz-attempts/{aid}/submit", json={
        "answers": [{"question_id": cau[0]["id"], "given_answer": "x"}],
    }, headers=h).json()

    r = client.patch(f"{API}/quiz-attempts/{aid}/answers/{nop['answers'][0]['id']}/grade",
                     json={"points_earned": -5}, headers=h)
    assert r.status_code == 422


# ─────────────────────────────────────────────────────────────
# Xem lại bài làm
# ─────────────────────────────────────────────────────────────

def test_xem_lai_bai_lam_cua_minh(client, make_teacher):
    _, h = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, h)

    aid = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]},
                      headers=h).json()["id"]
    client.post(f"{API}/quiz-attempts/{aid}/submit", json={
        "answers": [{"question_id": cau[0]["id"], "given_answer": "B"}],
    }, headers=h)

    r = client.get(f"{API}/quiz-attempts/{aid}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == aid


def test_khong_xem_duoc_bai_lam_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, hA)

    aid = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]},
                      headers=hA).json()["id"]

    r = client.get(f"{API}/quiz-attempts/{aid}", headers=hB)
    assert r.status_code == 403, f"B xem được bài làm của A (mã {r.status_code})"


def test_danh_sach_bai_lam_cua_minh_theo_de(client, make_teacher):
    _, h = make_teacher()
    quiz, cau = _de_da_xuat_ban(client, h)

    aid = client.post(f"{API}/quiz-attempts/start", json={"quiz_id": quiz["id"]},
                      headers=h).json()["id"]
    client.post(f"{API}/quiz-attempts/{aid}/submit", json={"answers": []}, headers=h)

    r = client.get(f"{API}/quiz-attempts/quiz/{quiz['id']}/my-attempts", headers=h)
    assert r.status_code == 200, r.text
    assert aid in {a["id"] for a in r.json()}
