"""
Pydantic schemas for classroom / assignment / submission features.
"""

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


# ─── Class ───────────────────────────────────────────────────

class ClassCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    subject: Optional[str] = None
    grade: Optional[int] = Field(None, ge=1, le=12)
    description: Optional[str] = None


class ClassUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    subject: Optional[str] = None
    grade: Optional[int] = Field(None, ge=1, le=12)
    description: Optional[str] = None
    is_active: Optional[bool] = None


class ClassResponse(BaseModel):
    id: int
    name: str
    subject: Optional[str]
    grade: Optional[int]
    description: Optional[str]
    code: str
    is_active: bool
    created_at: datetime
    member_count: Optional[int] = 0
    assignment_count: Optional[int] = 0

    model_config = {"from_attributes": True}


# ─── ClassMember ─────────────────────────────────────────────

class JoinClassRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=10)


class ClassMemberResponse(BaseModel):
    id: int
    student_id: int
    student_name: Optional[str] = None
    student_email: Optional[str] = None
    joined_at: datetime
    is_active: bool

    model_config = {"from_attributes": True}


# ─── Assignment ──────────────────────────────────────────────

class AssignmentCreate(BaseModel):
    class_id: int
    exam_id: Optional[int] = None
    title: str = Field(..., min_length=1, max_length=300)
    description: Optional[str] = None
    deadline: Optional[datetime] = None
    max_attempts: int = Field(default=3, ge=1, le=10)
    show_answer: bool = True


class AssignmentUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    description: Optional[str] = None
    deadline: Optional[datetime] = None
    max_attempts: Optional[int] = Field(None, ge=1, le=10)
    show_answer: Optional[bool] = None
    is_active: Optional[bool] = None


class AssignmentResponse(BaseModel):
    id: int
    class_id: int
    class_name: Optional[str] = None
    exam_id: Optional[int]
    title: str
    description: Optional[str]
    deadline: Optional[datetime]
    max_attempts: int
    show_answer: bool
    is_active: bool
    created_at: datetime
    submission_count: Optional[int] = 0
    completed_count: Optional[int] = 0

    model_config = {"from_attributes": True}


# ─── Send-to-multiple-classes convenience endpoint ───────────

class SendToClassesRequest(BaseModel):
    exam_id: int
    class_ids: List[int] = Field(..., min_length=1)
    title: str = Field(..., min_length=1, max_length=300)
    description: Optional[str] = None
    deadline: Optional[datetime] = None
    max_attempts: int = Field(default=3, ge=1, le=10)
    show_answer: bool = True


# ─── Submission ──────────────────────────────────────────────

class AnswerDetailIn(BaseModel):
    question_id: Optional[int] = None
    given_answer: Optional[str] = None
    is_correct: Optional[bool] = None
    time_ms: Optional[int] = None


class SubmissionCreate(BaseModel):
    assignment_id: int
    game_mode: Optional[str] = "quiz"
    time_spent_s: Optional[int] = None
    answers: List[AnswerDetailIn] = []


class SubmissionResponse(BaseModel):
    id: int
    assignment_id: int
    student_id: int
    student_name: Optional[str] = None
    score: Optional[int]
    total_q: int
    correct_q: int
    time_spent_s: Optional[int]
    attempt_no: int
    game_mode: Optional[str]
    status: str
    xp_earned: int
    submitted_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── StudentXP ───────────────────────────────────────────────

class StudentXPResponse(BaseModel):
    student_id: int
    total_xp: int
    level: int
    streak_days: int
    last_active: Optional[datetime]

    model_config = {"from_attributes": True}


# ─── Leaderboard ─────────────────────────────────────────────

class LeaderboardEntry(BaseModel):
    rank: int
    student_id: int
    student_name: str
    total_xp: int
    level: int
    streak_days: int
    is_me: bool = False


# ─── Class Analytics (teacher view) ──────────────────────────

class ClassAnalytics(BaseModel):
    class_id: int
    class_name: str
    total_students: int
    active_students: int          # submitted at least once in last 7 days
    total_assignments: int
    avg_score: Optional[float]
    completion_rate: Optional[float]  # % of students who completed latest assignment
    top_students: List[LeaderboardEntry] = []