"""
Curriculum model — chương trình GDPT 2018 (lớp 1-12, đa môn).
Mỗi row = 1 bài (lesson) thuộc một chương (chapter) của một môn + lớp.
Pre-seeded on first startup (hiện tại: Toán lớp 6-12).
"""

from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Index, UniqueConstraint
from app.db.base_class import Base


class Curriculum(Base):
    """
    Một bài/mục trong chương trình học.
    Mỗi row = 1 bài (lesson) thuộc một chương (chapter) của một môn (subject) + lớp (grade).
    """
    __tablename__ = "curriculum"

    id           = Column(Integer, primary_key=True, index=True)
    subject_code = Column(String(30), ForeignKey("subject.subject_code"), nullable=False, default="toan")
    grade        = Column(Integer, nullable=False)            # 1-12
    section_code = Column(String(30), nullable=False, default="")  # Cho KHTN: "vat-li", "hoa-hoc", "sinh-hoc"
    chapter_no   = Column(Integer, nullable=False)            # 1, 2, 3...
    chapter      = Column(String(300), nullable=False)        # "Chương I. Hàm số bậc hai..."
    lesson_no    = Column(Integer, nullable=False, default=0) # thứ tự bài trong chương
    lesson_title = Column(String(300), nullable=False)        # "§1. Hàm số bậc hai"
    is_active    = Column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("subject_code", "grade", "section_code", "chapter_no", "lesson_no",
                         name="uq_curriculum_subject"),
        Index("ix_curriculum_grade", "grade"),
        Index("ix_curriculum_grade_chapter", "grade", "chapter_no"),
        Index("ix_curriculum_subject_grade", "subject_code", "grade"),
    )


# ─── SGK Kết nối tri thức + GDPT 2018 — Toán học ────────────────────────────
# Nguồn: Sách giáo khoa Toán lớp 6-9 bộ Kết nối tri thức với cuộc sống
#         Chương trình GDPT 2018, Bộ GD&ĐT (lớp 10-12)

GDPT_2018_MATH: list[dict] = [

    # ══════════════════════════════════════════
    # LỚP 6 — Kết nối tri thức
    # ══════════════════════════════════════════
    # Chương I — Tập hợp các số tự nhiên
    {"grade":6,"chapter_no":1,"chapter":"Chương I. Tập hợp các số tự nhiên","lesson_no":1,"lesson_title":"Tập hợp"},
    {"grade":6,"chapter_no":1,"chapter":"Chương I. Tập hợp các số tự nhiên","lesson_no":2,"lesson_title":"Cách ghi số tự nhiên"},
    {"grade":6,"chapter_no":1,"chapter":"Chương I. Tập hợp các số tự nhiên","lesson_no":3,"lesson_title":"Thứ tự trong tập hợp các số tự nhiên"},
    {"grade":6,"chapter_no":1,"chapter":"Chương I. Tập hợp các số tự nhiên","lesson_no":4,"lesson_title":"Phép cộng và phép trừ số tự nhiên"},
    {"grade":6,"chapter_no":1,"chapter":"Chương I. Tập hợp các số tự nhiên","lesson_no":5,"lesson_title":"Phép nhân và phép chia số tự nhiên"},
    {"grade":6,"chapter_no":1,"chapter":"Chương I. Tập hợp các số tự nhiên","lesson_no":6,"lesson_title":"Lũy thừa với số mũ tự nhiên"},
    {"grade":6,"chapter_no":1,"chapter":"Chương I. Tập hợp các số tự nhiên","lesson_no":7,"lesson_title":"Thứ tự thực hiện các phép tính"},

    # Chương II — Tính chia hết trong tập hợp các số tự nhiên
    {"grade":6,"chapter_no":2,"chapter":"Chương II. Tính chia hết trong tập hợp các số tự nhiên","lesson_no":1,"lesson_title":"Quan hệ chia hết và tính chất"},
    {"grade":6,"chapter_no":2,"chapter":"Chương II. Tính chia hết trong tập hợp các số tự nhiên","lesson_no":2,"lesson_title":"Dấu hiệu chia hết"},
    {"grade":6,"chapter_no":2,"chapter":"Chương II. Tính chia hết trong tập hợp các số tự nhiên","lesson_no":3,"lesson_title":"Số nguyên tố"},
    {"grade":6,"chapter_no":2,"chapter":"Chương II. Tính chia hết trong tập hợp các số tự nhiên","lesson_no":4,"lesson_title":"Ước chung. Ước chung lớn nhất"},
    {"grade":6,"chapter_no":2,"chapter":"Chương II. Tính chia hết trong tập hợp các số tự nhiên","lesson_no":5,"lesson_title":"Bội chung. Bội chung nhỏ nhất"},

    # Chương III — Số nguyên
    {"grade":6,"chapter_no":3,"chapter":"Chương III. Số nguyên","lesson_no":1,"lesson_title":"Tập hợp các số nguyên"},
    {"grade":6,"chapter_no":3,"chapter":"Chương III. Số nguyên","lesson_no":2,"lesson_title":"Phép cộng và phép trừ số nguyên"},
    {"grade":6,"chapter_no":3,"chapter":"Chương III. Số nguyên","lesson_no":3,"lesson_title":"Quy tắc dấu ngoặc"},
    {"grade":6,"chapter_no":3,"chapter":"Chương III. Số nguyên","lesson_no":4,"lesson_title":"Phép nhân số nguyên"},
    {"grade":6,"chapter_no":3,"chapter":"Chương III. Số nguyên","lesson_no":5,"lesson_title":"Phép chia hết. Ước và bội của một số nguyên"},

    # Chương IV — Một số hình phẳng trong thực tiễn
    {"grade":6,"chapter_no":4,"chapter":"Chương IV. Một số hình phẳng trong thực tiễn","lesson_no":1,"lesson_title":"Hình tam giác đều. Hình vuông. Hình lục giác đều"},
    {"grade":6,"chapter_no":4,"chapter":"Chương IV. Một số hình phẳng trong thực tiễn","lesson_no":2,"lesson_title":"Hình chữ nhật. Hình thoi. Hình bình hành. Hình thang cân"},
    {"grade":6,"chapter_no":4,"chapter":"Chương IV. Một số hình phẳng trong thực tiễn","lesson_no":3,"lesson_title":"Chu vi và diện tích của một số tứ giác đã học"},

    # Chương V — Tính đối xứng của hình phẳng trong tự nhiên
    {"grade":6,"chapter_no":5,"chapter":"Chương V. Tính đối xứng của hình phẳng trong tự nhiên","lesson_no":1,"lesson_title":"Hình có trục đối xứng"},
    {"grade":6,"chapter_no":5,"chapter":"Chương V. Tính đối xứng của hình phẳng trong tự nhiên","lesson_no":2,"lesson_title":"Hình có tâm đối xứng"},

    # Chương VI — Phân số
    {"grade":6,"chapter_no":6,"chapter":"Chương VI. Phân số","lesson_no":1,"lesson_title":"Mở rộng phân số. Phân số bằng nhau"},
    {"grade":6,"chapter_no":6,"chapter":"Chương VI. Phân số","lesson_no":2,"lesson_title":"So sánh phân số. Hỗn số dương"},
    {"grade":6,"chapter_no":6,"chapter":"Chương VI. Phân số","lesson_no":3,"lesson_title":"Phép cộng và phép trừ phân số"},
    {"grade":6,"chapter_no":6,"chapter":"Chương VI. Phân số","lesson_no":4,"lesson_title":"Phép nhân và phép chia phân số"},
    {"grade":6,"chapter_no":6,"chapter":"Chương VI. Phân số","lesson_no":5,"lesson_title":"Hai bài toán về phân số"},

    # Chương VII — Số thập phân
    {"grade":6,"chapter_no":7,"chapter":"Chương VII. Số thập phân","lesson_no":1,"lesson_title":"Số thập phân"},
    {"grade":6,"chapter_no":7,"chapter":"Chương VII. Số thập phân","lesson_no":2,"lesson_title":"Tính toán với số thập phân"},
    {"grade":6,"chapter_no":7,"chapter":"Chương VII. Số thập phân","lesson_no":3,"lesson_title":"Làm tròn và ước lượng"},
    {"grade":6,"chapter_no":7,"chapter":"Chương VII. Số thập phân","lesson_no":4,"lesson_title":"Một số bài toán về tỉ số và tỉ số phần trăm"},

    # Chương VIII — Những hình hình học cơ bản
    {"grade":6,"chapter_no":8,"chapter":"Chương VIII. Những hình hình học cơ bản","lesson_no":1,"lesson_title":"Điểm và đường thẳng"},
    {"grade":6,"chapter_no":8,"chapter":"Chương VIII. Những hình hình học cơ bản","lesson_no":2,"lesson_title":"Điểm nằm giữa hai điểm. Tia"},
    {"grade":6,"chapter_no":8,"chapter":"Chương VIII. Những hình hình học cơ bản","lesson_no":3,"lesson_title":"Đoạn thẳng. Độ dài đoạn thẳng"},
    {"grade":6,"chapter_no":8,"chapter":"Chương VIII. Những hình hình học cơ bản","lesson_no":4,"lesson_title":"Trung điểm của đoạn thẳng"},
    {"grade":6,"chapter_no":8,"chapter":"Chương VIII. Những hình hình học cơ bản","lesson_no":5,"lesson_title":"Góc"},
    {"grade":6,"chapter_no":8,"chapter":"Chương VIII. Những hình hình học cơ bản","lesson_no":6,"lesson_title":"Số đo góc"},

    # Chương IX — Dữ liệu và xác suất thực nghiệm
    {"grade":6,"chapter_no":9,"chapter":"Chương IX. Dữ liệu và xác suất thực nghiệm","lesson_no":1,"lesson_title":"Dữ liệu và thu thập dữ liệu"},
    {"grade":6,"chapter_no":9,"chapter":"Chương IX. Dữ liệu và xác suất thực nghiệm","lesson_no":2,"lesson_title":"Bảng thống kê và biểu đồ tranh"},
    {"grade":6,"chapter_no":9,"chapter":"Chương IX. Dữ liệu và xác suất thực nghiệm","lesson_no":3,"lesson_title":"Biểu đồ cột"},
    {"grade":6,"chapter_no":9,"chapter":"Chương IX. Dữ liệu và xác suất thực nghiệm","lesson_no":4,"lesson_title":"Biểu đồ cột kép"},
    {"grade":6,"chapter_no":9,"chapter":"Chương IX. Dữ liệu và xác suất thực nghiệm","lesson_no":5,"lesson_title":"Kết quả có thể và sự kiện trong trò chơi, thí nghiệm"},
    {"grade":6,"chapter_no":9,"chapter":"Chương IX. Dữ liệu và xác suất thực nghiệm","lesson_no":6,"lesson_title":"Xác suất thực nghiệm"},

    # ══════════════════════════════════════════
    # LỚP 7 — Kết nối tri thức
    # ══════════════════════════════════════════
    # Chương I — Số hữu tỉ
    {"grade":7,"chapter_no":1,"chapter":"Chương I. Số hữu tỉ","lesson_no":1,"lesson_title":"Tập hợp các số hữu tỉ"},
    {"grade":7,"chapter_no":1,"chapter":"Chương I. Số hữu tỉ","lesson_no":2,"lesson_title":"Cộng, trừ, nhân, chia số hữu tỉ"},
    {"grade":7,"chapter_no":1,"chapter":"Chương I. Số hữu tỉ","lesson_no":3,"lesson_title":"Lũy thừa với số mũ tự nhiên của một số hữu tỉ"},
    {"grade":7,"chapter_no":1,"chapter":"Chương I. Số hữu tỉ","lesson_no":4,"lesson_title":"Thứ tự thực hiện các phép tính. Quy tắc chuyển vế"},

    # Chương II — Số thực
    {"grade":7,"chapter_no":2,"chapter":"Chương II. Số thực","lesson_no":1,"lesson_title":"Làm quen với số thập phân vô hạn tuần hoàn"},
    {"grade":7,"chapter_no":2,"chapter":"Chương II. Số thực","lesson_no":2,"lesson_title":"Số vô tỉ. Căn bậc hai số học"},
    {"grade":7,"chapter_no":2,"chapter":"Chương II. Số thực","lesson_no":3,"lesson_title":"Tập hợp các số thực"},

    # Chương III — Góc và đường thẳng song song
    {"grade":7,"chapter_no":3,"chapter":"Chương III. Góc và đường thẳng song song","lesson_no":1,"lesson_title":"Góc ở vị trí đặc biệt. Tia phân giác của một góc"},
    {"grade":7,"chapter_no":3,"chapter":"Chương III. Góc và đường thẳng song song","lesson_no":2,"lesson_title":"Hai đường thẳng song song và dấu hiệu nhận biết"},
    {"grade":7,"chapter_no":3,"chapter":"Chương III. Góc và đường thẳng song song","lesson_no":3,"lesson_title":"Tiên đề Euclid. Tính chất của hai đường thẳng song song"},
    {"grade":7,"chapter_no":3,"chapter":"Chương III. Góc và đường thẳng song song","lesson_no":4,"lesson_title":"Định lí và chứng minh định lí"},

    # Chương IV — Tam giác bằng nhau
    {"grade":7,"chapter_no":4,"chapter":"Chương IV. Tam giác bằng nhau","lesson_no":1,"lesson_title":"Tổng các góc trong một tam giác"},
    {"grade":7,"chapter_no":4,"chapter":"Chương IV. Tam giác bằng nhau","lesson_no":2,"lesson_title":"Hai tam giác bằng nhau. Trường hợp bằng nhau thứ nhất của tam giác"},
    {"grade":7,"chapter_no":4,"chapter":"Chương IV. Tam giác bằng nhau","lesson_no":3,"lesson_title":"Trường hợp bằng nhau thứ hai và thứ ba của tam giác"},
    {"grade":7,"chapter_no":4,"chapter":"Chương IV. Tam giác bằng nhau","lesson_no":4,"lesson_title":"Các trường hợp bằng nhau của tam giác vuông"},
    {"grade":7,"chapter_no":4,"chapter":"Chương IV. Tam giác bằng nhau","lesson_no":5,"lesson_title":"Tam giác cân. Đường trung trực của đoạn thẳng"},

    # Chương V — Thu thập và biểu diễn dữ liệu
    {"grade":7,"chapter_no":5,"chapter":"Chương V. Thu thập và biểu diễn dữ liệu","lesson_no":1,"lesson_title":"Thu thập và phân loại dữ liệu"},
    {"grade":7,"chapter_no":5,"chapter":"Chương V. Thu thập và biểu diễn dữ liệu","lesson_no":2,"lesson_title":"Biểu đồ hình quạt tròn"},
    {"grade":7,"chapter_no":5,"chapter":"Chương V. Thu thập và biểu diễn dữ liệu","lesson_no":3,"lesson_title":"Biểu đồ đoạn thẳng"},

    # Chương VI — Tỉ lệ thức và đại lượng tỉ lệ
    {"grade":7,"chapter_no":6,"chapter":"Chương VI. Tỉ lệ thức và đại lượng tỉ lệ","lesson_no":1,"lesson_title":"Tỉ lệ thức"},
    {"grade":7,"chapter_no":6,"chapter":"Chương VI. Tỉ lệ thức và đại lượng tỉ lệ","lesson_no":2,"lesson_title":"Tính chất của dãy tỉ số bằng nhau"},
    {"grade":7,"chapter_no":6,"chapter":"Chương VI. Tỉ lệ thức và đại lượng tỉ lệ","lesson_no":3,"lesson_title":"Đại lượng tỉ lệ thuận"},
    {"grade":7,"chapter_no":6,"chapter":"Chương VI. Tỉ lệ thức và đại lượng tỉ lệ","lesson_no":4,"lesson_title":"Đại lượng tỉ lệ nghịch"},

    # Chương VII — Biểu thức đại số và đa thức một biến
    {"grade":7,"chapter_no":7,"chapter":"Chương VII. Biểu thức đại số và đa thức một biến","lesson_no":1,"lesson_title":"Biểu thức đại số"},
    {"grade":7,"chapter_no":7,"chapter":"Chương VII. Biểu thức đại số và đa thức một biến","lesson_no":2,"lesson_title":"Đa thức một biến"},
    {"grade":7,"chapter_no":7,"chapter":"Chương VII. Biểu thức đại số và đa thức một biến","lesson_no":3,"lesson_title":"Phép cộng và phép trừ đa thức một biến"},
    {"grade":7,"chapter_no":7,"chapter":"Chương VII. Biểu thức đại số và đa thức một biến","lesson_no":4,"lesson_title":"Phép nhân đa thức một biến"},
    {"grade":7,"chapter_no":7,"chapter":"Chương VII. Biểu thức đại số và đa thức một biến","lesson_no":5,"lesson_title":"Phép chia đa thức một biến"},

    # Chương VIII — Làm quen với biến cố và xác suất của biến cố
    {"grade":7,"chapter_no":8,"chapter":"Chương VIII. Làm quen với biến cố và xác suất của biến cố","lesson_no":1,"lesson_title":"Làm quen với biến cố"},
    {"grade":7,"chapter_no":8,"chapter":"Chương VIII. Làm quen với biến cố và xác suất của biến cố","lesson_no":2,"lesson_title":"Làm quen với xác suất của biến cố"},

    # Chương IX — Quan hệ giữa các yếu tố trong một tam giác
    {"grade":7,"chapter_no":9,"chapter":"Chương IX. Quan hệ giữa các yếu tố trong một tam giác","lesson_no":1,"lesson_title":"Quan hệ giữa góc và cạnh đối diện trong một tam giác"},
    {"grade":7,"chapter_no":9,"chapter":"Chương IX. Quan hệ giữa các yếu tố trong một tam giác","lesson_no":2,"lesson_title":"Quan hệ giữa đường vuông góc và đường xiên"},
    {"grade":7,"chapter_no":9,"chapter":"Chương IX. Quan hệ giữa các yếu tố trong một tam giác","lesson_no":3,"lesson_title":"Quan hệ giữa ba cạnh của một tam giác"},
    {"grade":7,"chapter_no":9,"chapter":"Chương IX. Quan hệ giữa các yếu tố trong một tam giác","lesson_no":4,"lesson_title":"Sự đồng quy của ba đường trung tuyến, ba đường phân giác trong một tam giác"},
    {"grade":7,"chapter_no":9,"chapter":"Chương IX. Quan hệ giữa các yếu tố trong một tam giác","lesson_no":5,"lesson_title":"Sự đồng quy của ba đường trung trực, ba đường cao trong một tam giác"},

    # Chương X — Một số hình khối trong thực tiễn
    {"grade":7,"chapter_no":10,"chapter":"Chương X. Một số hình khối trong thực tiễn","lesson_no":1,"lesson_title":"Hình hộp chữ nhật và hình lập phương"},
    {"grade":7,"chapter_no":10,"chapter":"Chương X. Một số hình khối trong thực tiễn","lesson_no":2,"lesson_title":"Hình lăng trụ đứng tam giác và hình lăng trụ đứng tứ giác"},

    # ══════════════════════════════════════════
    # LỚP 8 — Kết nối tri thức
    # ══════════════════════════════════════════
    # Chương I — Đa thức
    {"grade":8,"chapter_no":1,"chapter":"Chương I. Đa thức","lesson_no":1,"lesson_title":"Đơn thức"},
    {"grade":8,"chapter_no":1,"chapter":"Chương I. Đa thức","lesson_no":2,"lesson_title":"Đa thức"},
    {"grade":8,"chapter_no":1,"chapter":"Chương I. Đa thức","lesson_no":3,"lesson_title":"Phép cộng và phép trừ đa thức"},
    {"grade":8,"chapter_no":1,"chapter":"Chương I. Đa thức","lesson_no":4,"lesson_title":"Phép nhân đa thức"},
    {"grade":8,"chapter_no":1,"chapter":"Chương I. Đa thức","lesson_no":5,"lesson_title":"Phép chia đa thức cho đơn thức"},

    # Chương II — Hằng đẳng thức đáng nhớ và ứng dụng
    {"grade":8,"chapter_no":2,"chapter":"Chương II. Hằng đẳng thức đáng nhớ và ứng dụng","lesson_no":1,"lesson_title":"Hiệu hai bình phương. Bình phương của một tổng hay một hiệu"},
    {"grade":8,"chapter_no":2,"chapter":"Chương II. Hằng đẳng thức đáng nhớ và ứng dụng","lesson_no":2,"lesson_title":"Lập phương của một tổng hay một hiệu"},
    {"grade":8,"chapter_no":2,"chapter":"Chương II. Hằng đẳng thức đáng nhớ và ứng dụng","lesson_no":3,"lesson_title":"Tổng và hiệu hai lập phương"},
    {"grade":8,"chapter_no":2,"chapter":"Chương II. Hằng đẳng thức đáng nhớ và ứng dụng","lesson_no":4,"lesson_title":"Phân tích đa thức thành nhân tử"},

    # Chương III — Tứ giác
    {"grade":8,"chapter_no":3,"chapter":"Chương III. Tứ giác","lesson_no":1,"lesson_title":"Tứ giác"},
    {"grade":8,"chapter_no":3,"chapter":"Chương III. Tứ giác","lesson_no":2,"lesson_title":"Hình thang cân"},
    {"grade":8,"chapter_no":3,"chapter":"Chương III. Tứ giác","lesson_no":3,"lesson_title":"Hình bình hành"},
    {"grade":8,"chapter_no":3,"chapter":"Chương III. Tứ giác","lesson_no":4,"lesson_title":"Hình chữ nhật"},
    {"grade":8,"chapter_no":3,"chapter":"Chương III. Tứ giác","lesson_no":5,"lesson_title":"Hình thoi và hình vuông"},

    # Chương IV — Định lí Thalès
    {"grade":8,"chapter_no":4,"chapter":"Chương IV. Định lí Thalès","lesson_no":1,"lesson_title":"Định lí Thalès trong tam giác"},
    {"grade":8,"chapter_no":4,"chapter":"Chương IV. Định lí Thalès","lesson_no":2,"lesson_title":"Đường trung bình của tam giác"},
    {"grade":8,"chapter_no":4,"chapter":"Chương IV. Định lí Thalès","lesson_no":3,"lesson_title":"Tính chất đường phân giác của tam giác"},

    # Chương V — Dữ liệu và biểu đồ
    {"grade":8,"chapter_no":5,"chapter":"Chương V. Dữ liệu và biểu đồ","lesson_no":1,"lesson_title":"Thu thập và phân loại dữ liệu"},
    {"grade":8,"chapter_no":5,"chapter":"Chương V. Dữ liệu và biểu đồ","lesson_no":2,"lesson_title":"Biểu diễn dữ liệu bằng bảng, biểu đồ"},
    {"grade":8,"chapter_no":5,"chapter":"Chương V. Dữ liệu và biểu đồ","lesson_no":3,"lesson_title":"Phân tích số liệu thống kê dựa vào biểu đồ"},

    # Chương VI — Phân thức đại số
    {"grade":8,"chapter_no":6,"chapter":"Chương VI. Phân thức đại số","lesson_no":1,"lesson_title":"Phân thức đại số"},
    {"grade":8,"chapter_no":6,"chapter":"Chương VI. Phân thức đại số","lesson_no":2,"lesson_title":"Tính chất cơ bản của phân thức đại số"},
    {"grade":8,"chapter_no":6,"chapter":"Chương VI. Phân thức đại số","lesson_no":3,"lesson_title":"Phép cộng và phép trừ phân thức đại số"},
    {"grade":8,"chapter_no":6,"chapter":"Chương VI. Phân thức đại số","lesson_no":4,"lesson_title":"Phép nhân và phép chia phân thức đại số"},

    # Chương VII — Phương trình bậc nhất và hàm số bậc nhất
    {"grade":8,"chapter_no":7,"chapter":"Chương VII. Phương trình bậc nhất và hàm số bậc nhất","lesson_no":1,"lesson_title":"Phương trình bậc nhất một ẩn"},
    {"grade":8,"chapter_no":7,"chapter":"Chương VII. Phương trình bậc nhất và hàm số bậc nhất","lesson_no":2,"lesson_title":"Giải bài toán bằng cách lập phương trình"},
    {"grade":8,"chapter_no":7,"chapter":"Chương VII. Phương trình bậc nhất và hàm số bậc nhất","lesson_no":3,"lesson_title":"Khái niệm hàm số và đồ thị của hàm số"},
    {"grade":8,"chapter_no":7,"chapter":"Chương VII. Phương trình bậc nhất và hàm số bậc nhất","lesson_no":4,"lesson_title":"Hàm số bậc nhất và đồ thị của hàm số bậc nhất"},
    {"grade":8,"chapter_no":7,"chapter":"Chương VII. Phương trình bậc nhất và hàm số bậc nhất","lesson_no":5,"lesson_title":"Hệ số góc của đường thẳng"},

    # Chương VIII — Mở đầu về tính xác suất của biến cố
    {"grade":8,"chapter_no":8,"chapter":"Chương VIII. Mở đầu về tính xác suất của biến cố","lesson_no":1,"lesson_title":"Kết quả có thể và kết quả thuận lợi"},
    {"grade":8,"chapter_no":8,"chapter":"Chương VIII. Mở đầu về tính xác suất của biến cố","lesson_no":2,"lesson_title":"Cách tính xác suất của biến cố bằng tỉ số"},
    {"grade":8,"chapter_no":8,"chapter":"Chương VIII. Mở đầu về tính xác suất của biến cố","lesson_no":3,"lesson_title":"Mối liên hệ giữa xác suất thực nghiệm với xác suất và ứng dụng"},

    # Chương IX — Tam giác đồng dạng
    {"grade":8,"chapter_no":9,"chapter":"Chương IX. Tam giác đồng dạng","lesson_no":1,"lesson_title":"Hai tam giác đồng dạng"},
    {"grade":8,"chapter_no":9,"chapter":"Chương IX. Tam giác đồng dạng","lesson_no":2,"lesson_title":"Ba trường hợp đồng dạng của hai tam giác"},
    {"grade":8,"chapter_no":9,"chapter":"Chương IX. Tam giác đồng dạng","lesson_no":3,"lesson_title":"Định lí Pythagore và ứng dụng"},
    {"grade":8,"chapter_no":9,"chapter":"Chương IX. Tam giác đồng dạng","lesson_no":4,"lesson_title":"Các trường hợp đồng dạng của hai tam giác vuông"},
    {"grade":8,"chapter_no":9,"chapter":"Chương IX. Tam giác đồng dạng","lesson_no":5,"lesson_title":"Hình đồng dạng"},

    # Chương X — Một số hình khối trong thực tiễn
    {"grade":8,"chapter_no":10,"chapter":"Chương X. Một số hình khối trong thực tiễn","lesson_no":1,"lesson_title":"Hình chóp tam giác đều"},
    {"grade":8,"chapter_no":10,"chapter":"Chương X. Một số hình khối trong thực tiễn","lesson_no":2,"lesson_title":"Hình chóp tứ giác đều"},

    # ══════════════════════════════════════════
    # LỚP 9 — Kết nối tri thức
    # ══════════════════════════════════════════
    # Chương I — Phương trình và hệ hai phương trình bậc nhất hai ẩn
    {"grade":9,"chapter_no":1,"chapter":"Chương I. Phương trình và hệ hai phương trình bậc nhất hai ẩn","lesson_no":1,"lesson_title":"Khái niệm phương trình và hệ hai phương trình bậc nhất hai ẩn"},
    {"grade":9,"chapter_no":1,"chapter":"Chương I. Phương trình và hệ hai phương trình bậc nhất hai ẩn","lesson_no":2,"lesson_title":"Giải hệ hai phương trình bậc nhất hai ẩn"},
    {"grade":9,"chapter_no":1,"chapter":"Chương I. Phương trình và hệ hai phương trình bậc nhất hai ẩn","lesson_no":3,"lesson_title":"Giải bài toán bằng cách lập hệ phương trình"},

    # Chương II — Phương trình và bất phương trình bậc nhất một ẩn
    {"grade":9,"chapter_no":2,"chapter":"Chương II. Phương trình và bất phương trình bậc nhất một ẩn","lesson_no":1,"lesson_title":"Phương trình quy về phương trình bậc nhất một ẩn"},
    {"grade":9,"chapter_no":2,"chapter":"Chương II. Phương trình và bất phương trình bậc nhất một ẩn","lesson_no":2,"lesson_title":"Bất đẳng thức và tính chất"},
    {"grade":9,"chapter_no":2,"chapter":"Chương II. Phương trình và bất phương trình bậc nhất một ẩn","lesson_no":3,"lesson_title":"Bất phương trình bậc nhất một ẩn"},

    # Chương III — Căn bậc hai và căn bậc ba
    {"grade":9,"chapter_no":3,"chapter":"Chương III. Căn bậc hai và căn bậc ba","lesson_no":1,"lesson_title":"Căn bậc hai và căn thức bậc hai"},
    {"grade":9,"chapter_no":3,"chapter":"Chương III. Căn bậc hai và căn bậc ba","lesson_no":2,"lesson_title":"Khai căn bậc hai với phép nhân và phép chia"},
    {"grade":9,"chapter_no":3,"chapter":"Chương III. Căn bậc hai và căn bậc ba","lesson_no":3,"lesson_title":"Biến đổi đơn giản và rút gọn biểu thức chứa căn thức bậc hai"},
    {"grade":9,"chapter_no":3,"chapter":"Chương III. Căn bậc hai và căn bậc ba","lesson_no":4,"lesson_title":"Căn bậc ba và căn thức bậc ba"},

    # Chương IV — Hệ thức lượng trong tam giác vuông
    {"grade":9,"chapter_no":4,"chapter":"Chương IV. Hệ thức lượng trong tam giác vuông","lesson_no":1,"lesson_title":"Tỉ số lượng giác của góc nhọn"},
    {"grade":9,"chapter_no":4,"chapter":"Chương IV. Hệ thức lượng trong tam giác vuông","lesson_no":2,"lesson_title":"Một số hệ thức giữa cạnh, góc trong tam giác vuông và ứng dụng"},

    # Chương V — Đường tròn
    {"grade":9,"chapter_no":5,"chapter":"Chương V. Đường tròn","lesson_no":1,"lesson_title":"Mở đầu về đường tròn"},
    {"grade":9,"chapter_no":5,"chapter":"Chương V. Đường tròn","lesson_no":2,"lesson_title":"Cung và dây của một đường tròn"},
    {"grade":9,"chapter_no":5,"chapter":"Chương V. Đường tròn","lesson_no":3,"lesson_title":"Độ dài của cung tròn. Diện tích hình quạt tròn và hình vành khuyên"},
    {"grade":9,"chapter_no":5,"chapter":"Chương V. Đường tròn","lesson_no":4,"lesson_title":"Vị trí tương đối của đường thẳng và đường tròn"},
    {"grade":9,"chapter_no":5,"chapter":"Chương V. Đường tròn","lesson_no":5,"lesson_title":"Vị trí tương đối của hai đường tròn"},

    # Chương VI — Hàm số y = ax² và phương trình bậc hai một ẩn
    {"grade":9,"chapter_no":6,"chapter":"Chương VI. Hàm số y = ax² (a ≠ 0). Phương trình bậc hai một ẩn","lesson_no":1,"lesson_title":"Hàm số y = ax² (a ≠ 0)"},
    {"grade":9,"chapter_no":6,"chapter":"Chương VI. Hàm số y = ax² (a ≠ 0). Phương trình bậc hai một ẩn","lesson_no":2,"lesson_title":"Phương trình bậc hai một ẩn"},
    {"grade":9,"chapter_no":6,"chapter":"Chương VI. Hàm số y = ax² (a ≠ 0). Phương trình bậc hai một ẩn","lesson_no":3,"lesson_title":"Định lí Viète và ứng dụng"},
    {"grade":9,"chapter_no":6,"chapter":"Chương VI. Hàm số y = ax² (a ≠ 0). Phương trình bậc hai một ẩn","lesson_no":4,"lesson_title":"Giải bài toán bằng cách lập phương trình bậc hai"},

    # Chương VII — Tần số và tần số tương đối
    {"grade":9,"chapter_no":7,"chapter":"Chương VII. Tần số và tần số tương đối","lesson_no":1,"lesson_title":"Bảng tần số và biểu đồ tần số"},
    {"grade":9,"chapter_no":7,"chapter":"Chương VII. Tần số và tần số tương đối","lesson_no":2,"lesson_title":"Bảng tần số tương đối và biểu đồ tần số tương đối"},
    {"grade":9,"chapter_no":7,"chapter":"Chương VII. Tần số và tần số tương đối","lesson_no":3,"lesson_title":"Bảng tần số, tần số tương đối ghép nhóm và biểu đồ"},

    # Chương VIII — Xác suất của biến cố trong một số mô hình xác suất đơn giản
    {"grade":9,"chapter_no":8,"chapter":"Chương VIII. Xác suất của biến cố trong một số mô hình xác suất đơn giản","lesson_no":1,"lesson_title":"Phép thử ngẫu nhiên và không gian mẫu"},
    {"grade":9,"chapter_no":8,"chapter":"Chương VIII. Xác suất của biến cố trong một số mô hình xác suất đơn giản","lesson_no":2,"lesson_title":"Xác suất của biến cố liên quan tới phép thử"},

    # Chương IX — Đường tròn ngoại tiếp và đường tròn nội tiếp
    {"grade":9,"chapter_no":9,"chapter":"Chương IX. Đường tròn ngoại tiếp và đường tròn nội tiếp","lesson_no":1,"lesson_title":"Góc nội tiếp"},
    {"grade":9,"chapter_no":9,"chapter":"Chương IX. Đường tròn ngoại tiếp và đường tròn nội tiếp","lesson_no":2,"lesson_title":"Đường tròn ngoại tiếp và đường tròn nội tiếp của một tam giác"},
    {"grade":9,"chapter_no":9,"chapter":"Chương IX. Đường tròn ngoại tiếp và đường tròn nội tiếp","lesson_no":3,"lesson_title":"Tứ giác nội tiếp"},
    {"grade":9,"chapter_no":9,"chapter":"Chương IX. Đường tròn ngoại tiếp và đường tròn nội tiếp","lesson_no":4,"lesson_title":"Đa giác đều"},

    # Chương X — Một số hình khối trong thực tiễn
    {"grade":9,"chapter_no":10,"chapter":"Chương X. Một số hình khối trong thực tiễn","lesson_no":1,"lesson_title":"Hình trụ và hình nón"},
    {"grade":9,"chapter_no":10,"chapter":"Chương X. Một số hình khối trong thực tiễn","lesson_no":2,"lesson_title":"Hình cầu"},


    # Chương I — Mệnh đề và tập hợp
    {"grade":10,"chapter_no":1,"chapter":"Chương I. Mệnh đề và tập hợp","lesson_no":1,"lesson_title":"Mệnh đề"},
    {"grade":10,"chapter_no":1,"chapter":"Chương I. Mệnh đề và tập hợp","lesson_no":2,"lesson_title":"Tập hợp"},
    {"grade":10,"chapter_no":1,"chapter":"Chương I. Mệnh đề và tập hợp","lesson_no":3,"lesson_title":"Các phép toán tập hợp"},
    {"grade":10,"chapter_no":1,"chapter":"Chương I. Mệnh đề và tập hợp","lesson_no":4,"lesson_title":"Các tập hợp số"},

    # Chương II — Bất phương trình và hệ bất phương trình bậc nhất hai ẩn
    {"grade":10,"chapter_no":2,"chapter":"Chương II. Bất phương trình và hệ bất phương trình bậc nhất hai ẩn","lesson_no":1,"lesson_title":"Bất phương trình bậc nhất một ẩn"},
    {"grade":10,"chapter_no":2,"chapter":"Chương II. Bất phương trình và hệ bất phương trình bậc nhất hai ẩn","lesson_no":2,"lesson_title":"Hệ bất phương trình bậc nhất một ẩn"},
    {"grade":10,"chapter_no":2,"chapter":"Chương II. Bất phương trình và hệ bất phương trình bậc nhất hai ẩn","lesson_no":3,"lesson_title":"Bất phương trình bậc nhất hai ẩn"},
    {"grade":10,"chapter_no":2,"chapter":"Chương II. Bất phương trình và hệ bất phương trình bậc nhất hai ẩn","lesson_no":4,"lesson_title":"Hệ bất phương trình bậc nhất hai ẩn"},

    # Chương III — Hệ thức lượng trong tam giác
    {"grade":10,"chapter_no":3,"chapter":"Chương III. Hệ thức lượng trong tam giác","lesson_no":1,"lesson_title":"Giá trị lượng giác của góc"},
    {"grade":10,"chapter_no":3,"chapter":"Chương III. Hệ thức lượng trong tam giác","lesson_no":2,"lesson_title":"Các hệ thức lượng trong tam giác"},
    {"grade":10,"chapter_no":3,"chapter":"Chương III. Hệ thức lượng trong tam giác","lesson_no":3,"lesson_title":"Định lý côsin và định lý sin"},
    {"grade":10,"chapter_no":3,"chapter":"Chương III. Hệ thức lượng trong tam giác","lesson_no":4,"lesson_title":"Giải tam giác — ứng dụng thực tế"},

    # Chương IV — Véctơ
    {"grade":10,"chapter_no":4,"chapter":"Chương IV. Véctơ","lesson_no":1,"lesson_title":"Khái niệm véctơ"},
    {"grade":10,"chapter_no":4,"chapter":"Chương IV. Véctơ","lesson_no":2,"lesson_title":"Tổng và hiệu hai véctơ"},
    {"grade":10,"chapter_no":4,"chapter":"Chương IV. Véctơ","lesson_no":3,"lesson_title":"Tích của véctơ với một số"},
    {"grade":10,"chapter_no":4,"chapter":"Chương IV. Véctơ","lesson_no":4,"lesson_title":"Tích vô hướng của hai véctơ"},

    # Chương V — Tọa độ trong mặt phẳng
    {"grade":10,"chapter_no":5,"chapter":"Chương V. Tọa độ trong mặt phẳng","lesson_no":1,"lesson_title":"Hệ trục tọa độ"},
    {"grade":10,"chapter_no":5,"chapter":"Chương V. Tọa độ trong mặt phẳng","lesson_no":2,"lesson_title":"Phương trình đường thẳng"},
    {"grade":10,"chapter_no":5,"chapter":"Chương V. Tọa độ trong mặt phẳng","lesson_no":3,"lesson_title":"Đường tròn"},
    {"grade":10,"chapter_no":5,"chapter":"Chương V. Tọa độ trong mặt phẳng","lesson_no":4,"lesson_title":"Ba đường conic"},

    # Chương VI — Thống kê và xác suất
    {"grade":10,"chapter_no":6,"chapter":"Chương VI. Thống kê và xác suất","lesson_no":1,"lesson_title":"Bảng tần số — tần suất ghép nhóm"},
    {"grade":10,"chapter_no":6,"chapter":"Chương VI. Thống kê và xác suất","lesson_no":2,"lesson_title":"Biểu đồ tần số — tần suất"},
    {"grade":10,"chapter_no":6,"chapter":"Chương VI. Thống kê và xác suất","lesson_no":3,"lesson_title":"Các số đặc trưng của mẫu số liệu"},
    {"grade":10,"chapter_no":6,"chapter":"Chương VI. Thống kê và xác suất","lesson_no":4,"lesson_title":"Biến cố và xác suất của biến cố"},
    {"grade":10,"chapter_no":6,"chapter":"Chương VI. Thống kê và xác suất","lesson_no":5,"lesson_title":"Cộng, nhân xác suất"},

    # ══════════════════════════════════════════
    # LỚP 11
    # ══════════════════════════════════════════
    # Chương I — Hàm số lượng giác và phương trình lượng giác
    {"grade":11,"chapter_no":1,"chapter":"Chương I. Hàm số lượng giác và phương trình lượng giác","lesson_no":1,"lesson_title":"Góc lượng giác và đo cung"},
    {"grade":11,"chapter_no":1,"chapter":"Chương I. Hàm số lượng giác và phương trình lượng giác","lesson_no":2,"lesson_title":"Giá trị lượng giác của góc lượng giác"},
    {"grade":11,"chapter_no":1,"chapter":"Chương I. Hàm số lượng giác và phương trình lượng giác","lesson_no":3,"lesson_title":"Các công thức lượng giác"},
    {"grade":11,"chapter_no":1,"chapter":"Chương I. Hàm số lượng giác và phương trình lượng giác","lesson_no":4,"lesson_title":"Hàm số lượng giác"},
    {"grade":11,"chapter_no":1,"chapter":"Chương I. Hàm số lượng giác và phương trình lượng giác","lesson_no":5,"lesson_title":"Phương trình lượng giác cơ bản"},
    {"grade":11,"chapter_no":1,"chapter":"Chương I. Hàm số lượng giác và phương trình lượng giác","lesson_no":6,"lesson_title":"Một số phương trình lượng giác thường gặp"},

    # Chương II — Dãy số — Cấp số cộng — Cấp số nhân
    {"grade":11,"chapter_no":2,"chapter":"Chương II. Dãy số — Cấp số cộng — Cấp số nhân","lesson_no":1,"lesson_title":"Dãy số"},
    {"grade":11,"chapter_no":2,"chapter":"Chương II. Dãy số — Cấp số cộng — Cấp số nhân","lesson_no":2,"lesson_title":"Cấp số cộng"},
    {"grade":11,"chapter_no":2,"chapter":"Chương II. Dãy số — Cấp số cộng — Cấp số nhân","lesson_no":3,"lesson_title":"Cấp số nhân"},

    # Chương III — Giới hạn
    {"grade":11,"chapter_no":3,"chapter":"Chương III. Giới hạn","lesson_no":1,"lesson_title":"Giới hạn của dãy số"},
    {"grade":11,"chapter_no":3,"chapter":"Chương III. Giới hạn","lesson_no":2,"lesson_title":"Giới hạn của hàm số"},
    {"grade":11,"chapter_no":3,"chapter":"Chương III. Giới hạn","lesson_no":3,"lesson_title":"Hàm số liên tục"},

    # Chương IV — Đạo hàm
    {"grade":11,"chapter_no":4,"chapter":"Chương IV. Đạo hàm","lesson_no":1,"lesson_title":"Định nghĩa và ý nghĩa của đạo hàm"},
    {"grade":11,"chapter_no":4,"chapter":"Chương IV. Đạo hàm","lesson_no":2,"lesson_title":"Quy tắc tính đạo hàm"},
    {"grade":11,"chapter_no":4,"chapter":"Chương IV. Đạo hàm","lesson_no":3,"lesson_title":"Đạo hàm của hàm số lượng giác"},
    {"grade":11,"chapter_no":4,"chapter":"Chương IV. Đạo hàm","lesson_no":4,"lesson_title":"Vi phân — Đạo hàm cấp hai"},

    # Chương V — Tổ hợp và xác suất
    {"grade":11,"chapter_no":5,"chapter":"Chương V. Tổ hợp và xác suất","lesson_no":1,"lesson_title":"Phép đếm"},
    {"grade":11,"chapter_no":5,"chapter":"Chương V. Tổ hợp và xác suất","lesson_no":2,"lesson_title":"Hoán vị — Chỉnh hợp — Tổ hợp"},
    {"grade":11,"chapter_no":5,"chapter":"Chương V. Tổ hợp và xác suất","lesson_no":3,"lesson_title":"Nhị thức Newton"},
    {"grade":11,"chapter_no":5,"chapter":"Chương V. Tổ hợp và xác suất","lesson_no":4,"lesson_title":"Xác suất của biến cố"},

    # Chương VI — Đường thẳng và mặt phẳng trong không gian
    {"grade":11,"chapter_no":6,"chapter":"Chương VI. Đường thẳng và mặt phẳng trong không gian","lesson_no":1,"lesson_title":"Điểm, đường thẳng, mặt phẳng trong không gian"},
    {"grade":11,"chapter_no":6,"chapter":"Chương VI. Đường thẳng và mặt phẳng trong không gian","lesson_no":2,"lesson_title":"Hai đường thẳng song song"},
    {"grade":11,"chapter_no":6,"chapter":"Chương VI. Đường thẳng và mặt phẳng trong không gian","lesson_no":3,"lesson_title":"Đường thẳng và mặt phẳng song song"},
    {"grade":11,"chapter_no":6,"chapter":"Chương VI. Đường thẳng và mặt phẳng trong không gian","lesson_no":4,"lesson_title":"Hai mặt phẳng song song"},

    # Chương VII — Quan hệ vuông góc trong không gian
    {"grade":11,"chapter_no":7,"chapter":"Chương VII. Quan hệ vuông góc trong không gian","lesson_no":1,"lesson_title":"Hai đường thẳng vuông góc"},
    {"grade":11,"chapter_no":7,"chapter":"Chương VII. Quan hệ vuông góc trong không gian","lesson_no":2,"lesson_title":"Đường thẳng vuông góc với mặt phẳng"},
    {"grade":11,"chapter_no":7,"chapter":"Chương VII. Quan hệ vuông góc trong không gian","lesson_no":3,"lesson_title":"Hai mặt phẳng vuông góc"},
    {"grade":11,"chapter_no":7,"chapter":"Chương VII. Quan hệ vuông góc trong không gian","lesson_no":4,"lesson_title":"Khoảng cách và góc trong không gian"},

    # ══════════════════════════════════════════
    # LỚP 12
    # ══════════════════════════════════════════
    # Chương I — Ứng dụng đạo hàm để khảo sát và vẽ đồ thị hàm số
    {"grade":12,"chapter_no":1,"chapter":"Chương I. Ứng dụng đạo hàm để khảo sát và vẽ đồ thị hàm số","lesson_no":1,"lesson_title":"Tính đơn điệu của hàm số"},
    {"grade":12,"chapter_no":1,"chapter":"Chương I. Ứng dụng đạo hàm để khảo sát và vẽ đồ thị hàm số","lesson_no":2,"lesson_title":"Cực trị của hàm số"},
    {"grade":12,"chapter_no":1,"chapter":"Chương I. Ứng dụng đạo hàm để khảo sát và vẽ đồ thị hàm số","lesson_no":3,"lesson_title":"Giá trị lớn nhất và giá trị nhỏ nhất của hàm số"},
    {"grade":12,"chapter_no":1,"chapter":"Chương I. Ứng dụng đạo hàm để khảo sát và vẽ đồ thị hàm số","lesson_no":4,"lesson_title":"Đường tiệm cận của đồ thị hàm số"},
    {"grade":12,"chapter_no":1,"chapter":"Chương I. Ứng dụng đạo hàm để khảo sát và vẽ đồ thị hàm số","lesson_no":5,"lesson_title":"Khảo sát sự biến thiên và vẽ đồ thị hàm số"},
    {"grade":12,"chapter_no":1,"chapter":"Chương I. Ứng dụng đạo hàm để khảo sát và vẽ đồ thị hàm số","lesson_no":6,"lesson_title":"Các bài toán liên quan đến đồ thị hàm số"},

    # Chương II — Hàm số lũy thừa — Hàm số mũ — Hàm số lôgarit
    {"grade":12,"chapter_no":2,"chapter":"Chương II. Hàm số lũy thừa — Hàm số mũ — Hàm số lôgarit","lesson_no":1,"lesson_title":"Lũy thừa"},
    {"grade":12,"chapter_no":2,"chapter":"Chương II. Hàm số lũy thừa — Hàm số mũ — Hàm số lôgarit","lesson_no":2,"lesson_title":"Hàm số lũy thừa"},
    {"grade":12,"chapter_no":2,"chapter":"Chương II. Hàm số lũy thừa — Hàm số mũ — Hàm số lôgarit","lesson_no":3,"lesson_title":"Lôgarit"},
    {"grade":12,"chapter_no":2,"chapter":"Chương II. Hàm số lũy thừa — Hàm số mũ — Hàm số lôgarit","lesson_no":4,"lesson_title":"Hàm số mũ — Hàm số lôgarit"},
    {"grade":12,"chapter_no":2,"chapter":"Chương II. Hàm số lũy thừa — Hàm số mũ — Hàm số lôgarit","lesson_no":5,"lesson_title":"Phương trình mũ và phương trình lôgarit"},
    {"grade":12,"chapter_no":2,"chapter":"Chương II. Hàm số lũy thừa — Hàm số mũ — Hàm số lôgarit","lesson_no":6,"lesson_title":"Bất phương trình mũ và bất phương trình lôgarit"},

    # Chương III — Nguyên hàm — Tích phân và ứng dụng
    {"grade":12,"chapter_no":3,"chapter":"Chương III. Nguyên hàm — Tích phân và ứng dụng","lesson_no":1,"lesson_title":"Nguyên hàm"},
    {"grade":12,"chapter_no":3,"chapter":"Chương III. Nguyên hàm — Tích phân và ứng dụng","lesson_no":2,"lesson_title":"Tích phân"},
    {"grade":12,"chapter_no":3,"chapter":"Chương III. Nguyên hàm — Tích phân và ứng dụng","lesson_no":3,"lesson_title":"Ứng dụng của tích phân trong hình học"},
    {"grade":12,"chapter_no":3,"chapter":"Chương III. Nguyên hàm — Tích phân và ứng dụng","lesson_no":4,"lesson_title":"Ứng dụng của tích phân trong vật lý và kinh tế"},

    # Chương IV — Số phức
    {"grade":12,"chapter_no":4,"chapter":"Chương IV. Số phức","lesson_no":1,"lesson_title":"Số phức"},
    {"grade":12,"chapter_no":4,"chapter":"Chương IV. Số phức","lesson_no":2,"lesson_title":"Cộng, trừ và nhân số phức"},
    {"grade":12,"chapter_no":4,"chapter":"Chương IV. Số phức","lesson_no":3,"lesson_title":"Phép chia số phức"},
    {"grade":12,"chapter_no":4,"chapter":"Chương IV. Số phức","lesson_no":4,"lesson_title":"Phương trình bậc hai với hệ số thực"},

    # Chương V — Thể tích khối đa diện
    {"grade":12,"chapter_no":5,"chapter":"Chương V. Thể tích khối đa diện","lesson_no":1,"lesson_title":"Khái niệm về khối đa diện"},
    {"grade":12,"chapter_no":5,"chapter":"Chương V. Thể tích khối đa diện","lesson_no":2,"lesson_title":"Khối lăng trụ và khối chóp"},
    {"grade":12,"chapter_no":5,"chapter":"Chương V. Thể tích khối đa diện","lesson_no":3,"lesson_title":"Thể tích của khối đa diện"},
    {"grade":12,"chapter_no":5,"chapter":"Chương V. Thể tích khối đa diện","lesson_no":4,"lesson_title":"Khối đa diện đều"},

    # Chương VI — Mặt nón, mặt trụ, mặt cầu
    {"grade":12,"chapter_no":6,"chapter":"Chương VI. Mặt nón, mặt trụ, mặt cầu","lesson_no":1,"lesson_title":"Mặt nón — Mặt trụ"},
    {"grade":12,"chapter_no":6,"chapter":"Chương VI. Mặt nón, mặt trụ, mặt cầu","lesson_no":2,"lesson_title":"Mặt cầu"},
    {"grade":12,"chapter_no":6,"chapter":"Chương VI. Mặt nón, mặt trụ, mặt cầu","lesson_no":3,"lesson_title":"Thể tích và diện tích khối nón, trụ, cầu"},

    # Chương VII — Phương pháp tọa độ trong không gian
    {"grade":12,"chapter_no":7,"chapter":"Chương VII. Phương pháp tọa độ trong không gian","lesson_no":1,"lesson_title":"Hệ tọa độ trong không gian"},
    {"grade":12,"chapter_no":7,"chapter":"Chương VII. Phương pháp tọa độ trong không gian","lesson_no":2,"lesson_title":"Phương trình mặt phẳng"},
    {"grade":12,"chapter_no":7,"chapter":"Chương VII. Phương pháp tọa độ trong không gian","lesson_no":3,"lesson_title":"Phương trình đường thẳng trong không gian"},
    {"grade":12,"chapter_no":7,"chapter":"Chương VII. Phương pháp tọa độ trong không gian","lesson_no":4,"lesson_title":"Khoảng cách và góc trong không gian (tọa độ)"},
]