"""
Seed dữ liệu 'yêu cầu cần đạt' (YCCĐ) môn Toán lớp 6 — Chương trình GDPT 2018
(Thông tư 32/2018/TT-BGDĐT).

Nguồn: Phụ lục chương trình môn Toán, phần Lớp 6 (3 mạch: Số và Đại số; Hình học
và Đo lường; Một số yếu tố Thống kê và Xác suất). Văn bản tóm lược trung thành
theo yêu cầu cần đạt của chương trình — KHÔNG copy SGK.

Mã code: TOAN6.<TOPIC>.<nn> (ổn định, dùng truy vết grounding trong KHBD).
`topic` đặt khớp tên chương KNTT để bước ánh xạ bán tự động dễ gom.

Idempotent: `seed_yccd_toan6` chỉ thêm mã chưa tồn tại.
"""

from __future__ import annotations
from typing import List, Dict

from sqlalchemy import text
from app.db.models.lesson_plan import Yccd


# strand = mạch nội dung; topic = chủ đề (≈ chương KNTT)
YCCD_TOAN_6: List[Dict] = [

    # ═══ MẠCH: SỐ VÀ ĐẠI SỐ ═══════════════════════════════════════════════
    # Số tự nhiên (Chương I)
    {"code": "TOAN6.STN.01", "strand": "Số và Đại số", "topic": "Số tự nhiên",
     "requirement": "Sử dụng được thuật ngữ tập hợp, phần tử thuộc/không thuộc một tập hợp; mô tả được tập hợp bằng cách liệt kê hoặc nêu tính chất đặc trưng."},
    {"code": "TOAN6.STN.02", "strand": "Số và Đại số", "topic": "Số tự nhiên",
     "requirement": "Đọc, viết được số tự nhiên trong hệ thập phân và số La Mã (không quá 30); biểu diễn được số tự nhiên trên tia số."},
    {"code": "TOAN6.STN.03", "strand": "Số và Đại số", "topic": "Số tự nhiên",
     "requirement": "Nhận biết được thứ tự trong tập hợp các số tự nhiên; so sánh được hai số tự nhiên."},
    {"code": "TOAN6.STN.04", "strand": "Số và Đại số", "topic": "Số tự nhiên",
     "requirement": "Thực hiện được phép cộng, trừ, nhân, chia số tự nhiên; vận dụng được các tính chất giao hoán, kết hợp, phân phối để tính nhẩm, tính nhanh hợp lí."},
    {"code": "TOAN6.STN.05", "strand": "Số và Đại số", "topic": "Số tự nhiên",
     "requirement": "Thực hiện được phép tính luỹ thừa với số mũ tự nhiên; nhân và chia hai luỹ thừa cùng cơ số."},
    {"code": "TOAN6.STN.06", "strand": "Số và Đại số", "topic": "Số tự nhiên",
     "requirement": "Vận dụng được thứ tự thực hiện các phép tính để tính giá trị biểu thức; giải quyết được vấn đề thực tiễn gắn với các phép tính về số tự nhiên."},

    # Tính chia hết (Chương II)
    {"code": "TOAN6.CHIAHET.01", "strand": "Số và Đại số", "topic": "Tính chia hết",
     "requirement": "Nhận biết được quan hệ chia hết, khái niệm ước và bội của một số tự nhiên."},
    {"code": "TOAN6.CHIAHET.02", "strand": "Số và Đại số", "topic": "Tính chia hết",
     "requirement": "Vận dụng được dấu hiệu chia hết cho 2, 5, 9, 3."},
    {"code": "TOAN6.CHIAHET.03", "strand": "Số và Đại số", "topic": "Tính chia hết",
     "requirement": "Nhận biết được số nguyên tố, hợp số; phân tích được một số tự nhiên ra thừa số nguyên tố."},
    {"code": "TOAN6.CHIAHET.04", "strand": "Số và Đại số", "topic": "Tính chia hết",
     "requirement": "Xác định được ước chung, ước chung lớn nhất, bội chung, bội chung nhỏ nhất; vận dụng vào rút gọn phân số và giải quyết vấn đề thực tiễn."},

    # Số nguyên (Chương III)
    {"code": "TOAN6.SONGUYEN.01", "strand": "Số và Đại số", "topic": "Số nguyên",
     "requirement": "Nhận biết được số nguyên âm, tập hợp các số nguyên; ý nghĩa của số nguyên âm trong thực tiễn; biểu diễn được số nguyên trên trục số."},
    {"code": "TOAN6.SONGUYEN.02", "strand": "Số và Đại số", "topic": "Số nguyên",
     "requirement": "Nhận biết được thứ tự trong tập hợp các số nguyên; so sánh được hai số nguyên."},
    {"code": "TOAN6.SONGUYEN.03", "strand": "Số và Đại số", "topic": "Số nguyên",
     "requirement": "Thực hiện được phép cộng, trừ, nhân, chia (chia hết) số nguyên; vận dụng được các tính chất của phép tính để tính hợp lí."},
    {"code": "TOAN6.SONGUYEN.04", "strand": "Số và Đại số", "topic": "Số nguyên",
     "requirement": "Nhận biết được quan hệ chia hết, ước và bội của một số nguyên; giải quyết được vấn đề thực tiễn gắn với phép tính về số nguyên."},

    # Phân số (Chương VI)
    {"code": "TOAN6.PHANSO.01", "strand": "Số và Đại số", "topic": "Phân số",
     "requirement": "Nhận biết được phân số với tử số và mẫu số là số nguyên; tính chất cơ bản của phân số; nhận biết được hai phân số bằng nhau và rút gọn được phân số."},
    {"code": "TOAN6.PHANSO.02", "strand": "Số và Đại số", "topic": "Phân số",
     "requirement": "So sánh được hai phân số; nhận biết và biểu diễn được hỗn số dương."},
    {"code": "TOAN6.PHANSO.03", "strand": "Số và Đại số", "topic": "Phân số",
     "requirement": "Thực hiện được phép cộng, phép trừ hai phân số; vận dụng tính chất của phép cộng để tính hợp lí."},
    {"code": "TOAN6.PHANSO.04", "strand": "Số và Đại số", "topic": "Phân số",
     "requirement": "Thực hiện được phép nhân, phép chia hai phân số; vận dụng được các tính chất để tính giá trị biểu thức một cách hợp lí."},
    {"code": "TOAN6.PHANSO.05", "strand": "Số và Đại số", "topic": "Phân số",
     "requirement": "Tính được giá trị phân số của một số cho trước và tìm được một số khi biết giá trị phân số của nó; giải quyết được vấn đề thực tiễn gắn với phép tính về phân số."},

    # Số thập phân (Chương VII)
    {"code": "TOAN6.STP.01", "strand": "Số và Đại số", "topic": "Số thập phân",
     "requirement": "Nhận biết được số thập phân âm, số đối của một số thập phân; so sánh được hai số thập phân."},
    {"code": "TOAN6.STP.02", "strand": "Số và Đại số", "topic": "Số thập phân",
     "requirement": "Thực hiện được phép cộng, trừ, nhân, chia số thập phân; vận dụng được các tính chất để tính hợp lí."},
    {"code": "TOAN6.STP.03", "strand": "Số và Đại số", "topic": "Số thập phân",
     "requirement": "Thực hiện được việc làm tròn số thập phân và ước lượng kết quả trong những trường hợp đơn giản."},
    {"code": "TOAN6.STP.04", "strand": "Số và Đại số", "topic": "Số thập phân",
     "requirement": "Tính được tỉ số và tỉ số phần trăm của hai đại lượng; giải quyết được một số bài toán thực tiễn gắn với tỉ số và tỉ số phần trăm."},

    # ═══ MẠCH: HÌNH HỌC VÀ ĐO LƯỜNG ═══════════════════════════════════════
    # Hình phẳng trong thực tiễn (Chương IV)
    {"code": "TOAN6.HINHPHANG.01", "strand": "Hình học và Đo lường", "topic": "Hình phẳng trong thực tiễn",
     "requirement": "Nhận dạng được tam giác đều, hình vuông, lục giác đều; mô tả được một số yếu tố cơ bản (cạnh, góc, đường chéo) của các hình đó."},
    {"code": "TOAN6.HINHPHANG.02", "strand": "Hình học và Đo lường", "topic": "Hình phẳng trong thực tiễn",
     "requirement": "Mô tả được một số yếu tố cơ bản của hình chữ nhật, hình thoi, hình bình hành, hình thang cân; vẽ được các hình đó."},
    {"code": "TOAN6.HINHPHANG.03", "strand": "Hình học và Đo lường", "topic": "Hình phẳng trong thực tiễn",
     "requirement": "Tính được chu vi và diện tích của hình chữ nhật, hình thoi, hình bình hành, hình thang; giải quyết được vấn đề thực tiễn gắn với chu vi và diện tích."},

    # Tính đối xứng (Chương V)
    {"code": "TOAN6.DOIXUNG.01", "strand": "Hình học và Đo lường", "topic": "Tính đối xứng",
     "requirement": "Nhận biết được trục đối xứng của một hình phẳng; nhận biết được những hình phẳng có trục đối xứng."},
    {"code": "TOAN6.DOIXUNG.02", "strand": "Hình học và Đo lường", "topic": "Tính đối xứng",
     "requirement": "Nhận biết được tâm đối xứng của một hình phẳng; nhận biết được những hình phẳng có tâm đối xứng."},
    {"code": "TOAN6.DOIXUNG.03", "strand": "Hình học và Đo lường", "topic": "Tính đối xứng",
     "requirement": "Nhận biết được tính đối xứng trong thế giới tự nhiên, nghệ thuật, kiến trúc; nhận biết được vẻ đẹp của thế giới tự nhiên biểu hiện qua tính đối xứng."},

    # Hình học phẳng cơ bản (Chương VIII)
    {"code": "TOAN6.HINHCOBAN.01", "strand": "Hình học và Đo lường", "topic": "Hình học cơ bản",
     "requirement": "Nhận biết được điểm, đường thẳng; điểm thuộc/không thuộc đường thẳng; ba điểm thẳng hàng; điểm nằm giữa hai điểm."},
    {"code": "TOAN6.HINHCOBAN.02", "strand": "Hình học và Đo lường", "topic": "Hình học cơ bản",
     "requirement": "Nhận biết được tia, đoạn thẳng; đo được độ dài đoạn thẳng; nhận biết được trung điểm của đoạn thẳng."},
    {"code": "TOAN6.HINHCOBAN.03", "strand": "Hình học và Đo lường", "topic": "Hình học cơ bản",
     "requirement": "Nhận biết được góc, điểm trong của một góc; đo được số đo của một góc bằng thước đo góc; nhận biết góc nhọn, góc vuông, góc tù, góc bẹt."},

    # ═══ MẠCH: THỐNG KÊ VÀ XÁC SUẤT ═══════════════════════════════════════
    # Thống kê (Chương IX)
    {"code": "TOAN6.THONGKE.01", "strand": "Thống kê và Xác suất", "topic": "Thống kê",
     "requirement": "Thực hiện được việc thu thập, phân loại dữ liệu theo các tiêu chí cho trước; nhận biết được tính hợp lí của dữ liệu."},
    {"code": "TOAN6.THONGKE.02", "strand": "Thống kê và Xác suất", "topic": "Thống kê",
     "requirement": "Đọc và mô tả được dữ liệu ở dạng bảng thống kê, biểu đồ tranh, biểu đồ cột, biểu đồ cột kép."},
    {"code": "TOAN6.THONGKE.03", "strand": "Thống kê và Xác suất", "topic": "Thống kê",
     "requirement": "Lựa chọn và biểu diễn được dữ liệu vào bảng, biểu đồ thích hợp; nhận ra được vấn đề hoặc quy luật đơn giản từ việc phân tích biểu đồ, bảng số liệu."},

    # Xác suất thực nghiệm (Chương IX)
    {"code": "TOAN6.XACSUAT.01", "strand": "Thống kê và Xác suất", "topic": "Xác suất thực nghiệm",
     "requirement": "Làm quen với mô hình xác suất trong một số trò chơi, thí nghiệm đơn giản; liệt kê được các kết quả có thể xảy ra."},
    {"code": "TOAN6.XACSUAT.02", "strand": "Thống kê và Xác suất", "topic": "Xác suất thực nghiệm",
     "requirement": "Kiểm đếm được số lần lặp lại của một khả năng trong thí nghiệm lặp; tính được xác suất thực nghiệm của một sự kiện."},
]


async def seed_yccd_toan6(session) -> int:
    """Idempotent: thêm các YCCĐ Toán 6 chưa tồn tại (theo `code`). Trả số mã đã thêm."""
    existing = {
        row[0]
        for row in (await session.execute(text("SELECT code FROM yccd"))).fetchall()
    }
    added = 0
    for row in YCCD_TOAN_6:
        if row["code"] in existing:
            continue
        session.add(Yccd(
            code=row["code"],
            subject_code="toan",
            grade=6,
            strand=row["strand"],
            topic=row["topic"],
            requirement=row["requirement"],
        ))
        added += 1
    if added:
        await session.commit()
    return added
