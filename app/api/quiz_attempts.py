"""
Quiz Attempt API — start attempt, submit answers, get results.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_active_user, get_optional_user
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.quiz import Quiz, QuizQuestion
from app.db.models.quiz_attempt import QuizAttempt, QuizAnswer
from app.db.models.quiz import QuizTheory, QuizTheorySection
from app.schemas.quiz import (
    StartAttemptRequest, SubmitAttemptRequest,
    QuizAttemptResponse, QuizAnswerResponse, HintResponse,
    GradeAnswerRequest, FinalizeGradingRequest,
)
from app.services.quiz_grader import grade_question

logger = logging.getLogger(__name__)
router = APIRouter()


def _now():
    return datetime.now(timezone.utc)


@router.post("/start", response_model=QuizAttemptResponse, status_code=201)
async def start_attempt(
    data: StartAttemptRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Start a new quiz attempt. Auth optional — anonymous guests get student_id=NULL."""
    # Verify quiz exists and is published (or owned by user)
    result = await db.execute(select(Quiz).where(Quiz.id == data.quiz_id))
    quiz = result.scalars().first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if quiz.status != "published":
        if not user or quiz.created_by_id != user.id:
            raise HTTPException(status_code=403, detail="Quiz not available")

    user_id = user.id if user else None

    # Auto-abandon all stale in-progress attempts by this user
    if user_id:
        stale = await db.execute(
            select(QuizAttempt).where(
                QuizAttempt.student_id == user_id,
                QuizAttempt.status == "in_progress",
            )
        )
        for old in stale.scalars().all():
            old.status = "abandoned"
            old.submitted_at = _now()
        await db.flush()

    # Check retake limits (only for logged-in users)
    settings = quiz.settings or {}
    if user_id:
        if not settings.get("allow_retake", True):
            existing = await db.execute(
                select(func.count(QuizAttempt.id)).where(
                    QuizAttempt.quiz_id == data.quiz_id,
                    QuizAttempt.student_id == user_id,
                    QuizAttempt.status == "completed",
                )
            )
            if (existing.scalar() or 0) > 0:
                raise HTTPException(status_code=400, detail="Retakes not allowed for this quiz")

        max_retakes = settings.get("max_retakes")
        if max_retakes is not None:
            existing = await db.execute(
                select(func.count(QuizAttempt.id)).where(
                    QuizAttempt.quiz_id == data.quiz_id,
                    QuizAttempt.student_id == user_id,
                    QuizAttempt.status == "completed",
                )
            )
            if (existing.scalar() or 0) >= max_retakes:
                raise HTTPException(status_code=400, detail=f"Maximum {max_retakes} attempts reached")

    # Determine attempt number
    attempt_no = 1
    if user_id:
        count_result = await db.execute(
            select(func.count(QuizAttempt.id)).where(
                QuizAttempt.quiz_id == data.quiz_id,
                QuizAttempt.student_id == user_id,
            )
        )
        attempt_no = (count_result.scalar() or 0) + 1

    # ── Question selection (difficulty-based random) ──
    selected_ids = None
    total_q = quiz.question_count

    selection_count = settings.get("question_selection_count")
    if selection_count and selection_count > 0:
        q_result = await db.execute(
            select(QuizQuestion).where(QuizQuestion.quiz_id == data.quiz_id)
        )
        all_questions = q_result.scalars().all()

        if selection_count < len(all_questions):
            from app.services.quiz_selector import select_questions
            distribution = settings.get("difficulty_distribution")
            selected = select_questions(all_questions, selection_count, distribution)
            selected_ids = [q.id for q in selected]
            total_q = len(selected)
        else:
            total_q = len(all_questions)

    attempt = QuizAttempt(
        quiz_id=data.quiz_id,
        student_id=user_id,
        assignment_id=data.assignment_id,
        attempt_no=attempt_no,
        status="in_progress",
        total_questions=total_q,
        selected_question_ids=selected_ids,
    )
    db.add(attempt)
    await db.commit()

    # Reload with answers relationship to satisfy response schema
    result = await db.execute(
        select(QuizAttempt)
        .options(selectinload(QuizAttempt.answers))
        .where(QuizAttempt.id == attempt.id)
    )
    return result.scalars().first()


@router.post("/{attempt_id}/submit", response_model=QuizAttemptResponse)
async def submit_attempt(
    attempt_id: int,
    data: SubmitAttemptRequest,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Submit answers for a quiz attempt and get graded results. Auth optional for anonymous attempts."""
    # Load attempt — match by student_id (or NULL for anonymous)
    user_id = user.id if user else None
    if user_id:
        result = await db.execute(
            select(QuizAttempt).where(
                QuizAttempt.id == attempt_id,
                QuizAttempt.student_id == user_id,
            )
        )
    else:
        # Anonymous: just look up by ID + student_id IS NULL
        result = await db.execute(
            select(QuizAttempt).where(
                QuizAttempt.id == attempt_id,
                QuizAttempt.student_id.is_(None),
            )
        )
    attempt = result.scalars().first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.status != "in_progress":
        raise HTTPException(status_code=400, detail="Attempt already submitted")

    # Load quiz questions for grading
    q_result = await db.execute(
        select(QuizQuestion).where(QuizQuestion.quiz_id == attempt.quiz_id)
    )
    questions_map = {q.id: q for q in q_result.scalars().all()}

    # Filter to selected questions only (if random selection was used)
    if attempt.selected_question_ids:
        selected_set = set(attempt.selected_question_ids)
        questions_map = {qid: q for qid, q in questions_map.items() if qid in selected_set}

    # Load quiz for settings (hint penalties, negative scoring, grading mode)
    quiz_result = await db.execute(select(Quiz).where(Quiz.id == attempt.quiz_id))
    quiz = quiz_result.scalars().first()
    settings = quiz.settings or {}
    hint_penalties = settings.get("hint_penalty", {})
    negative_scoring = settings.get("negative_scoring", False)
    grading_mode = settings.get("grading_mode", "auto")

    # max_possible is based on ALL selected questions, not just answered ones
    max_possible = sum(float(q.points) for q in questions_map.values())

    # Grade each answer
    total_earned = 0.0
    correct_count = 0
    is_manual = grading_mode == "manual"

    for ans in data.answers:
        qq = questions_map.get(ans.question_id)
        if not qq:
            continue

        q_points = float(qq.points)

        if is_manual:
            # Manual mode: save answers without grading — teacher will grade later
            qa = QuizAnswer(
                attempt_id=attempt.id,
                question_id=ans.question_id,
                given_answer=ans.given_answer,
                is_correct=None,
                points_earned=0,
                time_ms=ans.time_ms,
                hint_used=ans.hint_used,
                hint_level=ans.hint_level,
            )
            db.add(qa)
        else:
            # Auto mode: grade immediately
            grading = grade_question(
                question_type=qq.type,
                correct_answer=qq.answer,
                given_answer=ans.given_answer,
                points=q_points,
                scoring=qq.scoring,
                choices=qq.choices,
                items=qq.items,
            )

            earned = grading["points_earned"]

            # Negative scoring: wrong answer deducts 0.5 points, skip = 0
            if negative_scoring and ans.given_answer is not None and not grading["is_correct"]:
                earned = -0.5

            # Apply hint penalty (only on positive earnings)
            if ans.hint_used and ans.hint_level > 0 and earned > 0:
                penalty_key = f"level_{ans.hint_level}"
                penalty_pct = hint_penalties.get(penalty_key, 0)
                earned = earned * (1 - penalty_pct)

            if grading["is_correct"]:
                correct_count += 1
            total_earned += earned

            # Save answer
            qa = QuizAnswer(
                attempt_id=attempt.id,
                question_id=ans.question_id,
                given_answer=ans.given_answer,
                is_correct=grading["is_correct"],
                points_earned=round(earned, 2),
                time_ms=ans.time_ms,
                hint_used=ans.hint_used,
                hint_level=ans.hint_level,
            )
            db.add(qa)

    # Update attempt
    attempt.submitted_at = _now()
    if is_manual:
        attempt.status = "pending_review"
        attempt.score = None
        attempt.max_score = round(max_possible, 2)
        attempt.percentage = None
        attempt.correct_count = 0
        attempt.passed = None
    else:
        attempt.status = "completed"
        attempt.score = round(total_earned, 2)
        attempt.max_score = round(max_possible, 2)
        attempt.percentage = round(max(0, total_earned / max_possible * 100) if max_possible > 0 else 0, 2)
        attempt.correct_count = correct_count

        # Check passing
        passing_score = settings.get("passing_score")
        if passing_score is not None:
            if settings.get("passing_score_type", "points") == "percentage":
                attempt.passed = float(attempt.percentage) >= passing_score
            else:
                attempt.passed = float(attempt.score) >= passing_score

    # Calculate time spent
    if attempt.started_at:
        delta = attempt.submitted_at - attempt.started_at
        attempt.time_spent_s = int(delta.total_seconds())

    await db.commit()

    # Reload with answers
    result = await db.execute(
        select(QuizAttempt)
        .options(selectinload(QuizAttempt.answers))
        .where(QuizAttempt.id == attempt.id)
    )
    attempt_obj = result.scalars().first()

    # Build response with correct_answer + explanation per answer
    enriched_answers = []
    for a in attempt_obj.answers:
        qq = questions_map.get(a.question_id)
        explanation = None
        correct_answer = None
        if qq and not is_manual:
            correct_answer = qq.answer
            if qq.solution and isinstance(qq.solution, dict):
                explanation = qq.solution.get("explanation")
        enriched_answers.append(QuizAnswerResponse(
            id=a.id,
            question_id=a.question_id,
            given_answer=a.given_answer,
            is_correct=a.is_correct,
            points_earned=float(a.points_earned or 0),
            time_ms=a.time_ms,
            hint_used=a.hint_used or False,
            hint_level=a.hint_level or 0,
            correct_answer=correct_answer,
            explanation=explanation,
            teacher_comment=a.teacher_comment,
        ))

    return QuizAttemptResponse(
        id=attempt_obj.id,
        quiz_id=attempt_obj.quiz_id,
        student_id=attempt_obj.student_id,
        assignment_id=attempt_obj.assignment_id,
        attempt_no=attempt_obj.attempt_no,
        status=attempt_obj.status,
        score=float(attempt_obj.score) if attempt_obj.score is not None else None,
        max_score=float(attempt_obj.max_score) if attempt_obj.max_score is not None else None,
        percentage=float(attempt_obj.percentage) if attempt_obj.percentage is not None else None,
        passed=attempt_obj.passed,
        total_questions=attempt_obj.total_questions,
        correct_count=attempt_obj.correct_count,
        time_spent_s=attempt_obj.time_spent_s,
        xp_earned=attempt_obj.xp_earned or 0,
        selected_question_ids=attempt_obj.selected_question_ids,
        graded_by_id=attempt_obj.graded_by_id,
        graded_at=attempt_obj.graded_at,
        started_at=attempt_obj.started_at,
        submitted_at=attempt_obj.submitted_at,
        answers=enriched_answers,
    )


@router.get("/{attempt_id}", response_model=QuizAttemptResponse)
async def get_attempt(
    attempt_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Get attempt details with answers."""
    result = await db.execute(
        select(QuizAttempt)
        .options(selectinload(QuizAttempt.answers))
        .where(QuizAttempt.id == attempt_id)
    )
    attempt = result.scalars().first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")

    # Only allow student or quiz owner to view
    if attempt.student_id != user.id:
        quiz_result = await db.execute(select(Quiz).where(Quiz.id == attempt.quiz_id))
        quiz = quiz_result.scalars().first()
        if not quiz or quiz.created_by_id != user.id:
            raise HTTPException(status_code=403, detail="Not authorized")

    return attempt


@router.get("/quiz/{quiz_id}/my-attempts", response_model=list[QuizAttemptResponse])
async def my_attempts(
    quiz_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Get current user's attempts for a quiz."""
    result = await db.execute(
        select(QuizAttempt)
        .options(selectinload(QuizAttempt.answers))
        .where(
            QuizAttempt.quiz_id == quiz_id,
            QuizAttempt.student_id == user.id,
        )
        .order_by(QuizAttempt.attempt_no.desc())
    )
    return result.scalars().all()


@router.get("/{attempt_id}/hint/{question_id}", response_model=HintResponse)
async def get_hint(
    attempt_id: int,
    question_id: int,
    level: int = Query(..., ge=1, le=3),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Get a hint for a question within an in-progress attempt.

    Levels:
      1 — Theory section (free)
      2 — Solution steps (-25%)
      3 — Correct answer (-50%)
    """
    user_id = user.id if user else None

    # Load attempt
    if user_id:
        result = await db.execute(
            select(QuizAttempt).where(
                QuizAttempt.id == attempt_id,
                QuizAttempt.student_id == user_id,
            )
        )
    else:
        result = await db.execute(
            select(QuizAttempt).where(
                QuizAttempt.id == attempt_id,
                QuizAttempt.student_id.is_(None),
            )
        )
    attempt = result.scalars().first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.status != "in_progress":
        raise HTTPException(status_code=400, detail="Attempt is not in progress")

    # Verify question belongs to this attempt
    if attempt.selected_question_ids and question_id not in attempt.selected_question_ids:
        raise HTTPException(status_code=404, detail="Question not in this attempt")

    # Load question
    qq_result = await db.execute(
        select(QuizQuestion).where(
            QuizQuestion.id == question_id,
            QuizQuestion.quiz_id == attempt.quiz_id,
        )
    )
    qq = qq_result.scalars().first()
    if not qq:
        raise HTTPException(status_code=404, detail="Question not found")

    hint = HintResponse(question_id=question_id, hint_level=level)

    if level >= 1:
        # Level 1: Theory section
        if qq.hint_section_id:
            sec_result = await db.execute(
                select(QuizTheorySection).where(QuizTheorySection.id == qq.hint_section_id)
            )
            section = sec_result.scalars().first()
            if section:
                # Get parent theory title
                t_result = await db.execute(
                    select(QuizTheory.title).where(QuizTheory.id == section.theory_id)
                )
                hint.theory_title = t_result.scalar()
                hint.theory_content = section.content

    if level >= 2:
        # Level 2: Solution steps only (no explanation)
        if qq.solution and isinstance(qq.solution, dict):
            from app.schemas.quiz import SolutionData
            try:
                steps_only = {**qq.solution, "explanation": None}
                hint.solution = SolutionData(**steps_only)
            except Exception:
                hint.solution = None

    if level >= 3:
        # Level 3: Correct answer + explanation
        hint.answer = qq.answer
        if qq.solution and isinstance(qq.solution, dict):
            hint.explanation = qq.solution.get("explanation")

    return hint


# ─── Teacher Manual Grading ──────────────────────────────────────────────────

@router.get("/quiz/{quiz_id}/pending-review", response_model=list[QuizAttemptResponse])
async def get_pending_review_attempts(
    quiz_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Get all attempts pending teacher review for a quiz. Only quiz owner."""
    quiz_result = await db.execute(select(Quiz).where(Quiz.id == quiz_id))
    quiz = quiz_result.scalars().first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if quiz.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only quiz owner can review attempts")

    result = await db.execute(
        select(QuizAttempt)
        .options(selectinload(QuizAttempt.answers))
        .where(
            QuizAttempt.quiz_id == quiz_id,
            QuizAttempt.status == "pending_review",
        )
        .order_by(QuizAttempt.submitted_at.asc())
    )
    return result.scalars().all()


@router.patch(
    "/{attempt_id}/answers/{answer_id}/grade",
    response_model=QuizAnswerResponse,
)
async def grade_single_answer(
    attempt_id: int,
    answer_id: int,
    data: GradeAnswerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Teacher grades a single answer within a pending_review attempt."""
    # Load attempt
    result = await db.execute(
        select(QuizAttempt).where(QuizAttempt.id == attempt_id)
    )
    attempt = result.scalars().first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.status != "pending_review":
        raise HTTPException(status_code=400, detail="Attempt is not pending review")

    # Verify teacher owns the quiz
    quiz_result = await db.execute(select(Quiz).where(Quiz.id == attempt.quiz_id))
    quiz = quiz_result.scalars().first()
    if not quiz or quiz.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only quiz owner can grade")

    # Load the answer
    ans_result = await db.execute(
        select(QuizAnswer).where(
            QuizAnswer.id == answer_id,
            QuizAnswer.attempt_id == attempt_id,
        )
    )
    answer = ans_result.scalars().first()
    if not answer:
        raise HTTPException(status_code=404, detail="Answer not found")

    # Validate points don't exceed question max
    qq_result = await db.execute(
        select(QuizQuestion).where(QuizQuestion.id == answer.question_id)
    )
    qq = qq_result.scalars().first()
    if qq and data.points_earned > float(qq.points):
        raise HTTPException(
            status_code=400,
            detail=f"Points earned ({data.points_earned}) exceeds question max ({qq.points})",
        )

    # Update the answer
    answer.points_earned = round(data.points_earned, 2)
    if data.is_correct is not None:
        answer.is_correct = data.is_correct
    else:
        answer.is_correct = data.points_earned > 0
    if data.teacher_comment is not None:
        answer.teacher_comment = data.teacher_comment

    await db.commit()
    await db.refresh(answer)

    return QuizAnswerResponse(
        id=answer.id,
        question_id=answer.question_id,
        given_answer=answer.given_answer,
        is_correct=answer.is_correct,
        points_earned=float(answer.points_earned or 0),
        time_ms=answer.time_ms,
        hint_used=answer.hint_used or False,
        hint_level=answer.hint_level or 0,
        correct_answer=qq.answer if qq else None,
        explanation=None,
        teacher_comment=answer.teacher_comment,
    )


@router.post("/{attempt_id}/finalize-grading", response_model=QuizAttemptResponse)
async def finalize_grading(
    attempt_id: int,
    data: FinalizeGradingRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Finalize manual grading — calculate totals and mark attempt as completed."""
    result = await db.execute(
        select(QuizAttempt)
        .options(selectinload(QuizAttempt.answers))
        .where(QuizAttempt.id == attempt_id)
    )
    attempt = result.scalars().first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.status != "pending_review":
        raise HTTPException(status_code=400, detail="Attempt is not pending review")

    # Verify teacher owns the quiz
    quiz_result = await db.execute(select(Quiz).where(Quiz.id == attempt.quiz_id))
    quiz = quiz_result.scalars().first()
    if not quiz or quiz.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only quiz owner can finalize grading")

    settings = quiz.settings or {}

    # Calculate totals from graded answers
    total_earned = 0.0
    correct_count = 0
    for a in attempt.answers:
        total_earned += float(a.points_earned or 0)
        if a.is_correct:
            correct_count += 1

    max_possible = float(attempt.max_score or 0)

    attempt.status = "completed"
    attempt.score = round(total_earned, 2)
    attempt.percentage = round(
        max(0, total_earned / max_possible * 100) if max_possible > 0 else 0, 2
    )
    attempt.correct_count = correct_count
    attempt.graded_by_id = user.id
    attempt.graded_at = _now()

    # Check passing
    if data.passed is not None:
        attempt.passed = data.passed
    else:
        passing_score = settings.get("passing_score")
        if passing_score is not None:
            if settings.get("passing_score_type", "points") == "percentage":
                attempt.passed = float(attempt.percentage) >= passing_score
            else:
                attempt.passed = float(attempt.score) >= passing_score

    await db.commit()

    # Reload
    result = await db.execute(
        select(QuizAttempt)
        .options(selectinload(QuizAttempt.answers))
        .where(QuizAttempt.id == attempt.id)
    )
    return result.scalars().first()
