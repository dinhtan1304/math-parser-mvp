"""
difficulty_inferrer.py — Auto-infer độ khó từ câu tương tự trong ngân hàng.

Chạy background sau khi embed + similarity detection xong.

Logic:
  - Chỉ xử lý câu chưa có difficulty HOẶC AI gán "TH" (default khi không chắc)
  - Với mỗi câu đó, lấy top-3 câu tương tự nhất đã có difficulty rõ ràng
  - Weighted vote: score cao hơn → vote nặng hơn
  - Nếu confidence >= 0.6 → update difficulty
  - Ghi lại source để giải thích (audit trail)

Ví dụ:
  Câu mới (difficulty=TH, AI không chắc)
  → similar: [(id=7, diff=VD, score=0.91), (id=12, diff=VD, score=0.87), (id=3, diff=TH, score=0.84)]
  → weighted vote: VD=0.91+0.87=1.78, TH=0.84
  → confidence = 1.78/(1.78+0.84) = 0.68 >= 0.6
  → update difficulty = VD
"""

import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

VALID_DIFFICULTIES = {"NB", "TH", "VD", "VDC"}
CONFIDENCE_THRESHOLD = 0.6      # tối thiểu 60% weighted vote để tin
MIN_SIMILARITY_FOR_VOTE = 0.75  # chỉ dùng câu tương tự > 0.75 để vote


async def infer_difficulty_for_exam(
    db: AsyncSession,
    exam_id: int,
    user_id: int,
) -> int:
    """
    Với các câu trong exam_id thiếu/mơ hồ difficulty,
    dùng câu tương tự trong bank để infer.
    Trả về số câu được update.
    """
    # ── Lấy các câu trong exam này có difficulty cần xem lại ──
    # "TH" là default của AI khi không chắc → eligible nếu có similar mạnh hơn
    # NULL → chắc chắn eligible
    candidate_rows = (await db.execute(text("""
        SELECT q.id, q.question_text, q.difficulty
        FROM question q
        WHERE q.exam_id  = :eid
          AND q.user_id  = :uid
          AND (q.difficulty IS NULL OR q.difficulty = 'TH')
    """), {"eid": exam_id, "uid": user_id})).fetchall()

    if not candidate_rows:
        logger.debug(f"Exam {exam_id}: no difficulty candidates")
        return 0

    candidate_ids = [r[0] for r in candidate_rows]
    current_diff  = {r[0]: r[2] for r in candidate_rows}

    # ── Lấy câu tương tự cho các candidate (từ question_similarity) ──
    id_list = ",".join(str(i) for i in candidate_ids)
    similar_rows = (await db.execute(text(f"""
        SELECT qs.question_id, qs.similar_id, qs.score, q.difficulty
        FROM question_similarity qs
        JOIN question q ON q.id = qs.similar_id
        WHERE qs.question_id IN ({id_list})
          AND qs.score >= {MIN_SIMILARITY_FOR_VOTE}
          AND q.difficulty IS NOT NULL
          AND q.difficulty != ''
          AND q.exam_id != :eid
        ORDER BY qs.question_id, qs.score DESC
    """), {"eid": exam_id})).fetchall()

    if not similar_rows:
        logger.debug(f"Exam {exam_id}: no similar questions with difficulty for voting")
        return 0

    # ── Weighted voting per candidate ──
    # votes[question_id] = {difficulty: total_weight}
    votes: dict[int, dict[str, float]] = {}
    for qid, _sim_id, score, diff in similar_rows:
        if diff not in VALID_DIFFICULTIES:
            continue
        votes.setdefault(qid, {})
        votes[qid][diff] = votes[qid].get(diff, 0.0) + score

    # ── Compute winner + confidence ──
    updates = []  # [(question_id, new_difficulty, confidence, old_difficulty)]
    for qid, diff_weights in votes.items():
        if not diff_weights:
            continue
        total = sum(diff_weights.values())
        if total == 0:
            continue
        winner = max(diff_weights, key=diff_weights.get)
        confidence = diff_weights[winner] / total

        if confidence < CONFIDENCE_THRESHOLD:
            continue

        old_diff = current_diff.get(qid)
        # Only update if winner differs from current
        if winner != old_diff:
            updates.append((qid, winner, round(confidence, 3), old_diff))

    if not updates:
        logger.info(f"Exam {exam_id}: difficulty inference — no confident updates needed")
        return 0

    # ── Apply updates ──
    updated = 0
    for qid, new_diff, conf, old_diff in updates:
        try:
            await db.execute(text("""
                UPDATE question
                SET difficulty = :diff
                WHERE id = :qid
            """), {"diff": new_diff, "qid": qid})
            updated += 1
            logger.debug(
                f"  Q#{qid}: {old_diff or 'NULL'} → {new_diff} "
                f"(confidence={conf:.0%})"
            )
        except Exception as e:
            logger.warning(f"Difficulty update failed for Q#{qid}: {e}")

    try:
        await db.commit()
    except Exception as e:
        logger.warning(f"Difficulty infer commit failed: {e}")
        await db.rollback()
        return 0

    logger.info(
        f"Exam {exam_id}: difficulty inferred for {updated}/{len(candidate_rows)} candidates"
    )
    return updated