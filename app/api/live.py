"""
Live Battle API — real-time multiplayer quiz.

Teacher creates a room → students join via 6-char code →
teacher starts → questions broadcast → students answer → leaderboard updates.

REST endpoints (polling fallback):
  POST /live/create              — teacher creates room
  POST /live/{code}/join         — student joins room
  GET  /live/{code}/state        — current room state
  POST /live/{code}/start        — teacher starts session
  POST /live/{code}/next         — teacher advances to next question
  POST /live/{code}/end          — teacher ends session
  POST /live/{code}/answer       — student submits answer
  GET  /live/{code}/leaderboard  — current ranking

WebSocket:
  WS /live/ws/{room_code}?token= — real-time events
"""

import asyncio
import json
import logging
import random
import string
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.api import deps
from app.db.session import get_db, AsyncSessionLocal
from app.db.models.user import User
from app.db.models.classroom import Assignment, Class, ClassMember
from app.db.models.question import Question
from app.db.models.live_session import LiveSession, LiveParticipant, LiveAnswer

router = APIRouter()
logger = logging.getLogger(__name__)

# ── In-memory state (per process) ──────────────────────────────
# room_code → {"session_id", "assignment_id", "teacher_id", "status",
#               "current_idx", "questions", "connections": {user_id: WebSocket},
#               "student_names": {user_id: str}}
_live_rooms: Dict[str, dict] = {}
_rooms_lock = asyncio.Lock()


def _gen_room_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


async def _broadcast(room_code: str, event: str, data: dict, exclude_user: Optional[int] = None):
    """Send event to all connected WS clients in a room."""
    room = _live_rooms.get(room_code)
    if not room:
        return
    msg = json.dumps({"event": event, **data})
    dead = []
    for uid, ws in list(room["connections"].items()):
        if uid == exclude_user:
            continue
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(uid)
    for uid in dead:
        room["connections"].pop(uid, None)


# ── Schemas ─────────────────────────────────────────────────────

class CreateRoomRequest(BaseModel):
    assignment_id: int


class AnswerRequest(BaseModel):
    answer: str
    response_time_ms: int = 0


class LeaderboardEntry(BaseModel):
    student_id: int
    name: str
    score: int
    rank: int


class RoomStateResponse(BaseModel):
    room_code: str
    status: str
    current_question_idx: int
    total_questions: int
    participant_count: int
    leaderboard: List[LeaderboardEntry]
    current_question: Optional[dict] = None


# ── Helpers ─────────────────────────────────────────────────────

def _compute_leaderboard(room: dict) -> List[LeaderboardEntry]:
    scores: Dict[int, int] = room.get("scores", {})
    names: Dict[int, str] = room.get("student_names", {})
    sorted_entries = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [
        LeaderboardEntry(
            student_id=uid,
            name=names.get(uid, f"Học sinh {uid}"),
            score=score,
            rank=i + 1,
        )
        for i, (uid, score) in enumerate(sorted_entries)
    ]


def _question_for_client(room: dict, idx: int) -> Optional[dict]:
    qs = room.get("questions", [])
    if idx >= len(qs):
        return None
    q = qs[idx]
    return {
        "idx": idx,
        "question_id": q.id,
        "question_text": q.question_text,
        "answer": q.answer,
        "total": len(qs),
    }


async def _get_or_check_room(room_code: str) -> dict:
    room = _live_rooms.get(room_code)
    if not room:
        raise HTTPException(status_code=404, detail="Phòng không tồn tại hoặc đã kết thúc")
    return room


# ── REST Endpoints ───────────────────────────────────────────────

@router.post("/create", status_code=201)
async def create_room(
    payload: CreateRoomRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Teacher creates a live battle room."""
    assignment = await db.scalar(
        select(Assignment).where(
            Assignment.id == payload.assignment_id,
            Assignment.is_active == True,
        )
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Bài tập không tồn tại")

    cls = await db.get(Class, assignment.class_id)
    if not cls or cls.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Chỉ giáo viên mới tạo được phòng đấu")

    if not assignment.exam_id:
        raise HTTPException(status_code=422, detail="Bài tập chưa liên kết đề thi")

    # Load questions
    result = await db.execute(
        select(Question)
        .where(Question.exam_id == assignment.exam_id)
        .order_by(Question.question_order)
    )
    questions = list(result.scalars().all())
    if not questions:
        raise HTTPException(status_code=422, detail="Đề thi chưa có câu hỏi")

    # Generate unique room code
    for _ in range(10):
        code = _gen_room_code()
        if code not in _live_rooms:
            break

    # Persist to DB
    live_session = LiveSession(
        room_code=code,
        assignment_id=payload.assignment_id,
        teacher_id=current_user.id,
        status="waiting",
    )
    db.add(live_session)
    await db.commit()
    await db.refresh(live_session)

    # Store in memory
    async with _rooms_lock:
        _live_rooms[code] = {
            "session_id": live_session.id,
            "assignment_id": payload.assignment_id,
            "teacher_id": current_user.id,
            "status": "waiting",
            "current_idx": 0,
            "questions": questions,
            "connections": {},
            "scores": {},
            "student_names": {},
            "answered_current": set(),  # user_ids who answered current question
        }

    return {"room_code": code, "session_id": live_session.id, "total_questions": len(questions)}


@router.post("/{room_code}/join")
async def join_room(
    room_code: str,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Student joins a live room."""
    room = await _get_or_check_room(room_code)
    if room["status"] == "ended":
        raise HTTPException(status_code=400, detail="Phòng đã kết thúc")

    # Track participant in DB
    existing = await db.scalar(
        select(LiveParticipant).where(
            LiveParticipant.session_id == room["session_id"],
            LiveParticipant.student_id == current_user.id,
        )
    )
    if not existing:
        db.add(LiveParticipant(
            session_id=room["session_id"],
            student_id=current_user.id,
        ))
        await db.commit()

    # Update in-memory
    if current_user.id not in room["scores"]:
        room["scores"][current_user.id] = 0
    room["student_names"][current_user.id] = current_user.full_name or current_user.email.split("@")[0]

    # Notify teacher + other students
    await _broadcast(room_code, "student_joined", {
        "student_id": current_user.id,
        "name": room["student_names"][current_user.id],
        "participant_count": len(room["scores"]),
    })

    return {
        "room_code": room_code,
        "status": room["status"],
        "current_idx": room["current_idx"],
        "participant_count": len(room["scores"]),
    }


@router.get("/{room_code}/state", response_model=RoomStateResponse)
async def get_room_state(
    room_code: str,
    current_user: User = Depends(deps.get_current_user),
):
    """Polling fallback — get current room state."""
    room = await _get_or_check_room(room_code)
    return RoomStateResponse(
        room_code=room_code,
        status=room["status"],
        current_question_idx=room["current_idx"],
        total_questions=len(room["questions"]),
        participant_count=len(room["scores"]),
        leaderboard=_compute_leaderboard(room),
        current_question=_question_for_client(room, room["current_idx"]) if room["status"] == "active" else None,
    )


@router.post("/{room_code}/start")
async def start_session(
    room_code: str,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Teacher starts the live session."""
    room = await _get_or_check_room(room_code)
    if room["teacher_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="Chỉ giáo viên mới bắt đầu được")
    if room["status"] != "waiting":
        raise HTTPException(status_code=400, detail="Phòng đã bắt đầu hoặc kết thúc")

    room["status"] = "active"
    room["current_idx"] = 0
    room["answered_current"] = set()

    # Update DB
    result = await db.execute(
        select(LiveSession).where(LiveSession.room_code == room_code)
    )
    live = result.scalars().first()
    if live:
        from datetime import datetime, timezone
        live.status = "active"
        live.started_at = datetime.now(timezone.utc)
        await db.commit()

    q = _question_for_client(room, 0)
    await _broadcast(room_code, "question_start", {
        "question": q,
        "time_limit": 20,
    })

    return {"status": "active", "question": q}


@router.post("/{room_code}/next")
async def next_question(
    room_code: str,
    current_user: User = Depends(deps.get_current_user),
):
    """Teacher advances to next question."""
    room = await _get_or_check_room(room_code)
    if room["teacher_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="Chỉ giáo viên mới điều khiển được")

    current = room["current_idx"]
    total = len(room["questions"])

    # Broadcast end of current question with correct answer
    if current < total:
        q = room["questions"][current]
        lb = _compute_leaderboard(room)
        await _broadcast(room_code, "question_end", {
            "correct_answer": q.answer,
            "leaderboard": [e.model_dump() for e in lb],
        })

    next_idx = current + 1
    room["current_idx"] = next_idx
    room["answered_current"] = set()

    if next_idx >= total:
        return {"status": "last_question_ended"}

    q_next = _question_for_client(room, next_idx)
    await _broadcast(room_code, "question_start", {
        "question": q_next,
        "time_limit": 20,
    })

    return {"status": "active", "question": q_next, "idx": next_idx}


@router.post("/{room_code}/end")
async def end_session(
    room_code: str,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Teacher ends the live session."""
    room = await _get_or_check_room(room_code)
    if room["teacher_id"] != current_user.id:
        raise HTTPException(status_code=403, detail="Chỉ giáo viên mới kết thúc được")

    room["status"] = "ended"
    lb = _compute_leaderboard(room)
    await _broadcast(room_code, "session_ended", {
        "final_leaderboard": [e.model_dump() for e in lb],
    })

    # Update DB
    result = await db.execute(
        select(LiveSession).where(LiveSession.room_code == room_code)
    )
    live = result.scalars().first()
    if live:
        from datetime import datetime, timezone
        live.status = "ended"
        live.ended_at = datetime.now(timezone.utc)
        await db.commit()

    # Clean up memory after delay (give WS clients time to receive final event)
    async def _cleanup():
        await asyncio.sleep(30)
        _live_rooms.pop(room_code, None)
    asyncio.create_task(_cleanup())

    return {"status": "ended", "final_leaderboard": [e.model_dump() for e in lb]}


@router.post("/{room_code}/answer")
async def submit_answer(
    room_code: str,
    payload: AnswerRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Student submits an answer for the current question."""
    room = await _get_or_check_room(room_code)
    if room["status"] != "active":
        raise HTTPException(status_code=400, detail="Phòng chưa bắt đầu")

    if current_user.id in room["answered_current"]:
        return {"detail": "Đã trả lời rồi"}

    room["answered_current"].add(current_user.id)
    idx = room["current_idx"]
    questions = room["questions"]

    is_correct = False
    if idx < len(questions):
        q = questions[idx]
        is_correct = bool(q.answer and payload.answer.strip().lower() == q.answer.strip().lower())

    # Score: correct + speed bonus (max 1000 per question)
    if is_correct:
        speed_bonus = max(0, 1000 - payload.response_time_ms // 20)
        room["scores"][current_user.id] = room["scores"].get(current_user.id, 0) + 100 + speed_bonus

    # Persist to DB
    db.add(LiveAnswer(
        session_id=room["session_id"],
        student_id=current_user.id,
        question_idx=idx,
        answer=payload.answer,
        is_correct=is_correct,
        response_time_ms=payload.response_time_ms,
    ))
    await db.commit()

    # Notify teacher
    await _broadcast(room_code, "answer_submitted", {
        "student_id": current_user.id,
        "name": room["student_names"].get(current_user.id, ""),
        "is_correct": is_correct,
        "answered_count": len(room["answered_current"]),
        "total_participants": len(room["scores"]),
    }, exclude_user=current_user.id)

    # Wait — keep teacher connection open but don't broadcast to students until teacher goes next

    return {
        "is_correct": is_correct,
        "score": room["scores"].get(current_user.id, 0),
    }


@router.get("/{room_code}/leaderboard")
async def get_leaderboard(
    room_code: str,
    current_user: User = Depends(deps.get_current_user),
):
    """Current leaderboard for a room."""
    room = await _get_or_check_room(room_code)
    return {"leaderboard": [e.model_dump() for e in _compute_leaderboard(room)]}


# ── WebSocket Endpoint ──────────────────────────────────────────

@router.websocket("/ws/{room_code}")
async def live_ws(
    websocket: WebSocket,
    room_code: str,
    token: str = Query(...),
):
    """WebSocket connection for live battle real-time events.

    Authentication via ?token= query param (JWT).
    Events sent TO client: student_joined, question_start, question_end,
                           answer_submitted (teacher only), session_ended, ping
    Events received FROM client: "ping" (keepalive)
    """
    # Authenticate
    try:
        from app.core.security import decode_access_token
        user_id = decode_access_token(token)
        if not user_id:
            await websocket.close(code=4001)
            return
    except Exception:
        await websocket.close(code=4001)
        return

    room = _live_rooms.get(room_code)
    if not room:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    room["connections"][user_id] = websocket

    # Send current state immediately on connect
    try:
        state = {
            "event": "connected",
            "status": room["status"],
            "current_idx": room["current_idx"],
            "participant_count": len(room["scores"]),
            "leaderboard": [e.model_dump() for e in _compute_leaderboard(room)],
        }
        if room["status"] == "active":
            state["current_question"] = _question_for_client(room, room["current_idx"])
        await websocket.send_text(json.dumps(state))
    except Exception:
        pass

    try:
        while True:
            # Wait for any message from client (mainly ping keepalives)
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if msg == "ping":
                    await websocket.send_text(json.dumps({"event": "pong"}))
            except asyncio.TimeoutError:
                # Send server-side ping to keep connection alive
                try:
                    await websocket.send_text(json.dumps({"event": "ping"}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WS {room_code} user {user_id}: {e}")
    finally:
        room = _live_rooms.get(room_code)
        if room:
            room["connections"].pop(user_id, None)
