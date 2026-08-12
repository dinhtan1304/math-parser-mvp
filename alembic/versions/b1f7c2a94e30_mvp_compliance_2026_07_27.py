"""mvp compliance: nhãn AI + consent + soft-delete

Gộp 4 nhóm thay đổi của gói tuân thủ MVP vào 1 migration để chỉ phải migrate
một lần khi lên production:

  1. Nhãn nguồn gốc nội dung (Điều 44 Luật CN CNS + Luật AI):
     question / exam / lesson_plan += origin, ai_model, reviewed_by_user, reviewed_at
  2. Consent (Luật BVDLCN 91/2025): bảng consent_log + policy_version
  3. Soft-delete tài khoản: user.deleted_at
  4. Ẩn danh hóa bài làm cũ: submission.anonymized_at, quiz_attempt.anonymized_at

⚠️ TRƯỚC KHI CHẠY TRÊN PRODUCTION (Neon):
   Backfill ở bước 1 dựa trên heuristic. Migration in ra SỐ ROW của từng nhánh
   backfill. Hãy chạy trên một Neon branch (bản sao dump) trước, kiểm tra các con
   số đó có hợp lý không, RỒI mới apply lên prod.

   Heuristic cố ý bảo thủ: chỉ gắn AI_GENERATED khi chắc chắn (đề do luồng sinh
   đề AI tạo, filename bắt đầu bằng "AI:"). Câu có exam_id nhưng không phải đề AI
   → OCR_IMPORT. Câu không gắn đề (clone / lưu thủ công / lưu từ generate) → giữ
   HUMAN vì không đủ bằng chứng. Dán nhầm HUMAN→AI rủi ro hơn chiều ngược lại.

Revision ID: b1f7c2a94e30
Revises: 404cab2e5e0c
Create Date: 2026-07-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1f7c2a94e30'
down_revision: Union[str, Sequence[str], None] = '404cab2e5e0c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Các bảng nhận bộ cột nhãn nguồn gốc nội dung.
_ORIGIN_TABLES = ("question", "exam", "lesson_plan")


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_column(inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── 1. Cột nhãn nguồn gốc nội dung ────────────────────────────────
    for table in _ORIGIN_TABLES:
        if not _has_table(inspector, table):
            continue
        with op.batch_alter_table(table, schema=None) as batch_op:
            if not _has_column(inspector, table, "origin"):
                batch_op.add_column(sa.Column(
                    "origin", sa.String(length=20),
                    nullable=False, server_default="HUMAN",
                ))
            if not _has_column(inspector, table, "ai_model"):
                batch_op.add_column(sa.Column("ai_model", sa.String(length=60), nullable=True))
            if not _has_column(inspector, table, "reviewed_by_user"):
                batch_op.add_column(sa.Column(
                    "reviewed_by_user", sa.Boolean(),
                    nullable=False, server_default=sa.false(),
                ))
            if not _has_column(inspector, table, "reviewed_at"):
                batch_op.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))

    _backfill_origin(bind)

    # ── 2. Consent ────────────────────────────────────────────────────
    if not _has_table(inspector, "policy_version"):
        op.create_table(
            "policy_version",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("policy_type", sa.String(length=30), nullable=False),
            sa.Column("version", sa.String(length=20), nullable=False),
            sa.Column("effective_date", sa.DateTime(timezone=True), nullable=False),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("policy_version", schema=None) as batch_op:
            batch_op.create_index("ix_policy_version_id", ["id"], unique=False)
            batch_op.create_index("ix_policy_version_type_date", ["policy_type", "effective_date"], unique=False)

    if not _has_table(inspector, "consent_log"):
        op.create_table(
            "consent_log",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("consent_type", sa.String(length=30), nullable=False),
            sa.Column("policy_version", sa.String(length=20), nullable=False),
            sa.Column("action", sa.String(length=15), nullable=False),
            sa.Column("ip_address", sa.String(length=45), nullable=True),
            sa.Column("user_agent", sa.String(length=400), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        with op.batch_alter_table("consent_log", schema=None) as batch_op:
            batch_op.create_index("ix_consent_log_id", ["id"], unique=False)
            batch_op.create_index("ix_consent_log_created_at", ["created_at"], unique=False)
            batch_op.create_index("ix_consent_log_user_type", ["user_id", "consent_type", "created_at"], unique=False)

    # ── 3. Soft-delete tài khoản ──────────────────────────────────────
    if _has_table(inspector, "user") and not _has_column(inspector, "user", "deleted_at"):
        with op.batch_alter_table("user", schema=None) as batch_op:
            batch_op.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
            batch_op.add_column(sa.Column("delete_cancel_token", sa.String(), nullable=True))
            batch_op.create_index("ix_user_deleted_at", ["deleted_at"], unique=False)

    # ── 4. Ẩn danh hóa bài làm ────────────────────────────────────────
    # NB: QuizAttempt không khai __tablename__ → Base tự đặt tên "quizattempt".
    for table in ("submission", "quizattempt"):
        if _has_table(inspector, table) and not _has_column(inspector, table, "anonymized_at"):
            with op.batch_alter_table(table, schema=None) as batch_op:
                batch_op.add_column(sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True))


def _backfill_origin(bind) -> None:
    """Gán origin cho dữ liệu cũ + in số row mỗi nhánh để người vận hành duyệt."""
    inspector = sa.inspect(bind)
    if not _has_table(inspector, "question") or not _has_table(inspector, "exam"):
        return

    # exam: đề sinh bởi luồng AI (save_as_exam đặt filename "AI: <tiêu đề>").
    ai_exam_filter = "filename LIKE 'AI:%'"

    bind.execute(sa.text(
        f"UPDATE exam SET origin = 'AI_GENERATED' WHERE {ai_exam_filter}"
    ))
    bind.execute(sa.text(
        f"UPDATE exam SET origin = 'OCR_IMPORT' WHERE NOT ({ai_exam_filter})"
    ))

    # question: theo đề nguồn. Câu không gắn đề → giữ HUMAN (không đủ bằng chứng).
    bind.execute(sa.text(
        "UPDATE question SET origin = 'AI_GENERATED' "
        "WHERE exam_id IN (SELECT id FROM exam WHERE filename LIKE 'AI:%')"
    ))
    bind.execute(sa.text(
        "UPDATE question SET origin = 'OCR_IMPORT' "
        "WHERE exam_id IS NOT NULL "
        "AND exam_id IN (SELECT id FROM exam WHERE filename NOT LIKE 'AI:%')"
    ))

    # lesson_plan: đã sinh bằng AI; "reviewed" nghĩa là giáo viên đã duyệt.
    if _has_table(inspector, "lesson_plan"):
        bind.execute(sa.text(
            "UPDATE lesson_plan SET origin = 'AI_GENERATED', ai_model = model_used "
            "WHERE status IN ('generated', 'reviewed')"
        ))
        bind.execute(sa.text(
            "UPDATE lesson_plan SET origin = 'AI_ASSISTED', reviewed_by_user = true "
            "WHERE status = 'reviewed'"
        ))

    _report_counts(bind)


def _report_counts(bind) -> None:
    """In số row mỗi nhánh backfill — DUYỆT các con số này trước khi chạy prod."""
    inspector = sa.inspect(bind)
    print("\n=== Backfill nhãn nguồn gốc — kiểm tra trước khi apply lên production ===")
    for table in _ORIGIN_TABLES:
        if not _has_table(inspector, table):
            continue
        rows = bind.execute(sa.text(
            f"SELECT origin, COUNT(*) FROM {table} GROUP BY origin ORDER BY origin"
        )).fetchall()
        total = sum(r[1] for r in rows)
        detail = ", ".join(f"{r[0]}={r[1]}" for r in rows) or "(trống)"
        print(f"  {table:<12} tổng {total:>6}  |  {detail}")
    print("=== Nếu số liệu bất thường, hãy downgrade và chỉnh heuristic ===\n")


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # NB: QuizAttempt không khai __tablename__ → Base tự đặt tên "quizattempt".
    for table in ("submission", "quizattempt"):
        if _has_table(inspector, table) and _has_column(inspector, table, "anonymized_at"):
            with op.batch_alter_table(table, schema=None) as batch_op:
                batch_op.drop_column("anonymized_at")

    if _has_table(inspector, "user") and _has_column(inspector, "user", "deleted_at"):
        with op.batch_alter_table("user", schema=None) as batch_op:
            batch_op.drop_index("ix_user_deleted_at")
            batch_op.drop_column("delete_cancel_token")
            batch_op.drop_column("deleted_at")

    if _has_table(inspector, "consent_log"):
        op.drop_table("consent_log")
    if _has_table(inspector, "policy_version"):
        op.drop_table("policy_version")

    for table in _ORIGIN_TABLES:
        if not _has_table(inspector, table):
            continue
        with op.batch_alter_table(table, schema=None) as batch_op:
            for col in ("reviewed_at", "reviewed_by_user", "ai_model", "origin"):
                if _has_column(inspector, table, col):
                    batch_op.drop_column(col)
