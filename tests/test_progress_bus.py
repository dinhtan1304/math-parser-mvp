"""
Test cho cụm SSE + background task — thứ `api/parser.py` và `api/ielts_parser.py`
DÙNG CHUNG.

TẠI SAO FILE NÀY TỒN TẠI
Audit 2026-08-11 chỉ ra đây là cặp coupling chặt nhất repo:

    # app/api/ielts_parser.py
    from app.api.parser import _publish_progress, _background_tasks, UPLOAD_DIR

Một router import TÊN PRIVATE và BIẾN TRẠNG THÁI TOÀN CỤC của router khác.
`ielts_parser` không có test nào, nên mọi thay đổi cơ chế SSE / background task
trong `parser.py` đều có thể làm vỡ nó ÂM THẦM.

File này khóa hành vi của cụm đó TRƯỚC khi tách nó ra module riêng, để việc
tách được chứng minh là không đổi hành vi.

Test viết theo HÀNH VI, không theo vị trí import, nên sống sót qua refactor —
sau khi tách chỉ cần đổi dòng import ở đầu file.

Bổ sung cho tests/test_runtime_state.py (đã phủ publish/subscribe cơ bản +
unsubscribe idempotent).
"""

import asyncio

import pytest


# ─────────────────────────────────────────────────────────────
# Fan-out: nhiều người nghe cùng một exam
# ─────────────────────────────────────────────────────────────

def test_moi_nguoi_nghe_deu_nhan_duoc_su_kien():
    from app.api import parser as P

    async def run():
        q1 = await P._subscribe(5001)
        q2 = await P._subscribe(5001)
        q3 = await P._subscribe(5002)  # exam khác — KHÔNG được nhận

        P._publish_progress(5001, "progress", {"percent": 10})

        e1, m1 = await asyncio.wait_for(q1.get(), timeout=2.0)
        e2, m2 = await asyncio.wait_for(q2.get(), timeout=2.0)
        q3_rong = q3.empty()

        await P._unsubscribe(5001, q1)
        await P._unsubscribe(5001, q2)
        await P._unsubscribe(5002, q3)
        return e1, m1, e2, m2, q3_rong

    e1, m1, e2, m2, q3_rong = asyncio.run(run())
    assert e1 == e2 == "progress"
    assert '"percent": 10' in m1
    assert '"percent": 10' in m2
    assert q3_rong is True, "sự kiện của exam này rò sang exam khác"


def test_go_mot_nguoi_nghe_khong_anh_huong_nguoi_con_lai():
    from app.api import parser as P

    async def run():
        q1 = await P._subscribe(5003)
        q2 = await P._subscribe(5003)
        await P._unsubscribe(5003, q1)

        P._publish_progress(5003, "progress", {"percent": 55})
        e, m = await asyncio.wait_for(q2.get(), timeout=2.0)

        con_lai = len(P._progress_queues.get(5003, []))
        await P._unsubscribe(5003, q2)
        return e, m, con_lai

    e, m, con_lai = asyncio.run(run())
    assert e == "progress"
    assert '"percent": 55' in m
    assert con_lai == 1


def test_publish_khi_khong_ai_nghe_khong_nem_loi():
    """process_file phát tiến độ kể cả khi người dùng đã đóng tab."""
    from app.api import parser as P

    P._publish_progress(999999, "progress", {"percent": 1})  # không được nổ


# ─────────────────────────────────────────────────────────────
# Chống nghẽn: hàng đợi đầy thì bỏ sự kiện, KHÔNG chặn pipeline
# ─────────────────────────────────────────────────────────────

def test_hang_doi_day_thi_bo_su_kien_chu_khong_treo():
    """Client chậm KHÔNG được phép làm nghẽn luồng parse.

    Hàng đợi có maxsize=100. Phát quá số đó thì sự kiện thừa bị bỏ lặng lẽ —
    đây là chủ ý: mất một mốc tiến độ chấp nhận được, treo pipeline thì không.
    """
    from app.api import parser as P

    async def run():
        q = await P._subscribe(5004)
        for i in range(150):  # vượt maxsize
            P._publish_progress(5004, "progress", {"i": i})
        so_luong = q.qsize()
        await P._unsubscribe(5004, q)
        return so_luong

    so_luong = asyncio.run(run())
    assert so_luong == 100, f"phải chặn ở maxsize=100, thực tế {so_luong}"


def test_cac_loai_su_kien_deu_di_qua():
    """FE phân biệt 4 loại; đặc biệt stream_timeout KHÔNG phải lỗi.

    Xem mathplay-frontend/src/lib/api.ts subscribeProgress: stream_timeout chỉ
    đóng SSE, polling vẫn tiếp tục. Nếu nó bị gộp thành error_event thì FE sẽ
    dừng poll và người dùng thấy treo vĩnh viễn.
    """
    from app.api import parser as P

    async def run():
        q = await P._subscribe(5005)
        for ev in ("progress", "complete", "error_event", "stream_timeout", "quality_report"):
            P._publish_progress(5005, ev, {"ten": ev})
        nhan = [await asyncio.wait_for(q.get(), timeout=2.0) for _ in range(5)]
        await P._unsubscribe(5005, q)
        return [e for e, _ in nhan]

    assert asyncio.run(run()) == [
        "progress", "complete", "error_event", "stream_timeout", "quality_report",
    ]


# ─────────────────────────────────────────────────────────────
# Background task: rút cạn lúc tắt máy chủ
# ─────────────────────────────────────────────────────────────

def test_drain_cho_task_dang_chay_xong():
    """B4: lúc shutdown phải đợi index (FTS/embedding/similarity) chạy xong,
    nếu không câu hỏi vào ngân hàng mà thiếu index."""
    from app.api import parser as P

    async def run():
        xong = []

        async def viec():
            await asyncio.sleep(0.05)
            xong.append(1)

        t = asyncio.create_task(viec())
        P._background_tasks.add(t)
        t.add_done_callback(P._background_tasks.discard)

        so_task = await P.drain_background_tasks(timeout=5.0)
        return so_task, len(xong)

    so_task, so_xong = asyncio.run(run())
    assert so_task == 1
    assert so_xong == 1, "drain trả về nhưng task chưa chạy xong"


def test_drain_khong_co_gi_thi_tra_ve_0():
    from app.api import parser as P

    async def run():
        P._background_tasks.clear()
        return await P.drain_background_tasks(timeout=1.0)

    assert asyncio.run(run()) == 0


def test_drain_het_gio_thi_tra_ve_chu_khong_treo():
    """Task treo KHÔNG được giữ tiến trình sống mãi lúc shutdown."""
    from app.api import parser as P

    async def run():
        async def treo():
            await asyncio.sleep(30)

        t = asyncio.create_task(treo())
        P._background_tasks.add(t)
        try:
            batdau = asyncio.get_event_loop().time()
            so_task = await P.drain_background_tasks(timeout=0.2)
            mat = asyncio.get_event_loop().time() - batdau
            return so_task, mat
        finally:
            t.cancel()
            P._background_tasks.discard(t)

    so_task, mat = asyncio.run(run())
    assert so_task == 1
    assert mat < 5.0, f"drain phải bỏ cuộc sau timeout, mất {mat:.1f}s"


# ─────────────────────────────────────────────────────────────
# COUPLING: parser và ielts_parser PHẢI dùng chung một cơ chế
# ─────────────────────────────────────────────────────────────

def test_ielts_parser_dung_chung_co_che_phat_tien_do_voi_parser():
    """Hai router phải phát tiến độ qua CÙNG một đường.

    Nếu tách đôi, người dùng tải đề IELTS sẽ không thấy tiến độ (SSE nghe ở
    một chỗ, sự kiện phát ở chỗ khác) — hỏng âm thầm, không có lỗi nào.

    Test viết theo danh tính đối tượng nên đúng cả trước và sau khi tách cụm
    này ra module riêng: chỉ cần cả hai cùng trỏ về một nơi.
    """
    from app.api import parser as P
    from app.api import ielts_parser as IP

    assert IP._publish_progress is P._publish_progress, (
        "ielts_parser và parser phát tiến độ qua hai hàm khác nhau"
    )
    assert IP._background_tasks is P._background_tasks, (
        "hai router giữ hai sổ background task khác nhau → drain lúc shutdown "
        "sẽ bỏ sót một nửa"
    )


def test_ielts_parser_ghi_file_cung_thu_muc_voi_parser():
    """Cùng UPLOAD_DIR, nếu không thì dọn theo retention sẽ bỏ sót một nửa."""
    from app.api import parser as P
    from app.api import ielts_parser as IP

    assert IP.UPLOAD_DIR == P.UPLOAD_DIR


def test_su_kien_tu_ielts_parser_den_duoc_nguoi_nghe_cua_parser():
    """Kiểm chứng đầu-cuối: SSE đăng ký qua parser nhận được sự kiện do
    ielts_parser phát. Đây mới là điều thực sự quan trọng, chứ không phải
    hai module import từ đâu."""
    from app.api import parser as P
    from app.api import ielts_parser as IP

    async def run():
        q = await P._subscribe(5006)
        IP._publish_progress(5006, "progress", {"nguon": "ielts"})
        e, m = await asyncio.wait_for(q.get(), timeout=2.0)
        await P._unsubscribe(5006, q)
        return e, m

    e, m = asyncio.run(run())
    assert e == "progress"
    assert '"nguon": "ielts"' in m
