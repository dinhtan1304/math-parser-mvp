"""
Kênh phát tiến độ (SSE) + sổ theo dõi background task — dùng chung cho mọi
luồng parse.

TẠI SAO MODULE NÀY TỒN TẠI
Trước 2026-08-12 cụm này nằm trong `app/api/parser.py`, và `app/api/ielts_parser.py`
phải với sang lấy TÊN PRIVATE + BIẾN TRẠNG THÁI TOÀN CỤC của nó:

    from app.api.parser import _publish_progress, _background_tasks   # ❌

Đó là cặp coupling chặt nhất repo (audit 2026-08-11): một router phụ thuộc vào
nội tạng của router khác, mà `ielts_parser` lại không có test — nên đổi cơ chế
SSE trong `parser.py` sẽ làm vỡ nó âm thầm. Tách ra đây để cả hai router cùng
phụ thuộc vào một hợp đồng công khai, không ai phụ thuộc vào ai.

HAI ĐƯỜNG PHÁT SỰ KIỆN (loại trừ nhau)
  - Có Redis  : PUBLISH sang kênh "parser:{exam_id}" → mọi worker cùng nhận.
  - Không Redis: đẩy thẳng vào hàng đợi cục bộ (một tiến trình).
Hai đường loại trừ nhau nên client nằm cùng worker với bên phát KHÔNG bị nhận
trùng sự kiện.

Test: tests/test_progress_bus.py, tests/test_runtime_state.py
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, List

from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Sổ background task
# ─────────────────────────────────────────────────────────────
# Giữ tham chiếu tới task nền để chúng không bị garbage-collect giữa chừng
# (asyncio chỉ giữ weak reference tới task đang chạy).
_background_tasks: set[asyncio.Task] = set()


def track_task(task: asyncio.Task) -> asyncio.Task:
    """Ghi task vào sổ và tự gỡ khi xong. Trả lại chính task cho tiện nối chuỗi."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def drain_background_tasks(timeout: float = 20.0) -> int:
    """Lúc tắt máy chủ, đợi các task nền (FTS / embedding / similarity /
    difficulty) chạy xong để câu hỏi không vào ngân hàng mà thiếu index.

    Trả về số task đã đợi. Quá ``timeout`` thì bỏ cuộc và trả về — task treo
    KHÔNG được giữ tiến trình sống mãi.
    """
    pending = [t for t in list(_background_tasks) if not t.done()]
    if not pending:
        return 0
    try:
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            "drain_background_tasks: %d task(s) still running after %ss",
            len(pending), timeout,
        )
    return len(pending)


# ─────────────────────────────────────────────────────────────
# Kênh SSE
# ─────────────────────────────────────────────────────────────
# exam_id → danh sách hàng đợi của những người đang nghe (đường không-Redis).
_progress_queues: Dict[int, List[asyncio.Queue]] = {}
# Khoá bảo vệ việc thêm/bớt hàng đợi (tránh hỏng danh sách khi sửa đồng thời).
_queues_lock = asyncio.Lock()
# Cầu nối Redis theo từng người nghe: id(queue) → (pubsub, task chuyển tiếp)
_sse_redis_bridges: Dict[int, tuple] = {}

# Hàng đợi có trần: client chậm thì bỏ sự kiện, KHÔNG chặn luồng parse.
QUEUE_MAXSIZE = 100


async def _safe_publish(r, channel: str, payload: str) -> None:
    """Publish sang Redis kiểu cố-gắng-hết-sức (nuốt lỗi)."""
    try:
        await r.publish(channel, payload)
    except Exception as e:
        logger.debug("SSE redis publish failed: %s", e)


def publish_progress(exam_id: int, event: str, data: dict) -> None:
    """Phát một sự kiện tiến độ tới mọi client SSE đang nghe ``exam_id``.

    Không bao giờ ném lỗi: phát tiến độ mà làm vỡ luồng parse thì còn tệ hơn
    mất một mốc tiến độ. Gọi được cả khi không ai đang nghe.

    ``event`` là một trong: progress | complete | error_event | stream_timeout |
    quality_report. Lưu ý ``stream_timeout`` KHÔNG phải lỗi — nó chỉ báo SSE im
    lặng quá lâu; frontend đóng SSE nhưng vẫn polling tiếp.
    """
    r = get_redis()
    if r is not None:
        payload = json.dumps({"event": event, "data": data}, ensure_ascii=False)
        track_task(asyncio.create_task(_safe_publish(r, f"parser:{exam_id}", payload)))
        return

    # Fan-out cục bộ (một worker)
    queues = _progress_queues.get(exam_id, [])
    msg = json.dumps(data, ensure_ascii=False)
    for q in list(queues):  # duyệt bản sao: tránh sửa danh sách giữa vòng lặp
        try:
            q.put_nowait((event, msg))
        except asyncio.QueueFull:
            pass  # client quá chậm → bỏ sự kiện này


async def _pubsub_forward(pubsub, q: asyncio.Queue) -> None:
    """Chuyển tiếp thông điệp Redis pub/sub của MỘT người nghe vào hàng đợi của họ."""
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                obj = json.loads(message["data"])
                event = obj.get("event", "progress")
                payload = json.dumps(obj.get("data", {}), ensure_ascii=False)
                q.put_nowait((event, payload))
            except asyncio.QueueFull:
                pass
            except Exception:
                pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug("SSE pubsub forward ended: %s", e)


async def subscribe(exam_id: int) -> asyncio.Queue:
    """Đăng ký nghe tiến độ của một exam. Trả về hàng đợi để đọc sự kiện."""
    q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)

    r = get_redis()
    if r is not None:
        try:
            pubsub = r.pubsub()
            await pubsub.subscribe(f"parser:{exam_id}")
            task = asyncio.create_task(_pubsub_forward(pubsub, q))
            _sse_redis_bridges[id(q)] = (pubsub, task)
            return q
        except Exception as e:
            logger.warning("Redis SSE subscribe failed, using in-memory: %s", e)

    async with _queues_lock:
        _progress_queues.setdefault(exam_id, []).append(q)
    return q


async def unsubscribe(exam_id: int, q: asyncio.Queue) -> None:
    """Ngừng nghe. Gọi nhiều lần cũng không sao (idempotent)."""
    bridge = _sse_redis_bridges.pop(id(q), None)
    if bridge is not None:
        pubsub, task = bridge
        task.cancel()
        try:
            await pubsub.unsubscribe(f"parser:{exam_id}")
            await pubsub.aclose()
        except Exception:
            pass
        return

    async with _queues_lock:
        queues = _progress_queues.get(exam_id, [])
        if q in queues:
            queues.remove(q)
        if not queues and exam_id in _progress_queues:
            del _progress_queues[exam_id]
