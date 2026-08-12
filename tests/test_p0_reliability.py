"""
P0 reliability fixes (2026-07-10):

  1. `_ocr_limited` — khi caller cancel (asyncio.wait_for timeout) mà engine
     thread/subprocess còn chạy, semaphore PHẢI được giữ đến khi engine thật sự
     xong. Nếu nhả sớm, upload kế tiếp start engine nặng thứ 2 chạy chồng → OOM.
  2. Budget threading — parser truyền OCR budget xuống `_run_mineru_pipeline`,
     cap `OCR_BENCHMARK_PER_ENGINE_TIMEOUT` để subprocess MinerU bị kill đúng
     hạn (process-tree kill trong run_command_template) thay vì orphan 30 phút.
  3. Orphan exam recovery — boot đánh `failed` mọi exam kẹt pending/processing
     (BackgroundTasks không sống sót qua restart).
"""
import asyncio
import os
import sqlite3

import pytest


# ─── 1. Deferred semaphore release on caller cancellation ────────────────────

def test_ocr_semaphore_held_until_engine_finishes_after_cancel():
    import app.services.local_ocr_service as svc

    async def run():
        svc._ocr_semaphore = None  # fresh semaphore bound to this loop
        gate = asyncio.Event()
        started = asyncio.Event()

        @svc._ocr_limited
        async def slow_engine():
            started.set()
            await gate.wait()   # engine "thread" still busy after caller gives up
            return "done"

        @svc._ocr_limited
        async def second_engine():
            return "second"

        t1 = asyncio.ensure_future(slow_engine())
        await started.wait()

        # Caller gives up — simulates parser's asyncio.wait_for backstop firing.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(t1, timeout=0.05)

        # Semaphore must STILL be held (engine not finished) → second upload blocked.
        t2 = asyncio.ensure_future(second_engine())
        await asyncio.sleep(0.05)
        assert not t2.done(), "second engine started while first still running — OOM guard broken"

        # Engine finally finishes → deferred release unblocks the queue.
        gate.set()
        assert await asyncio.wait_for(t2, timeout=2) == "second"

    try:
        asyncio.run(run())
    finally:
        import app.services.local_ocr_service as svc2
        svc2._ocr_semaphore = None  # don't leak loop-bound state to other tests


def test_ocr_semaphore_releases_normally_without_cancel():
    import app.services.local_ocr_service as svc

    async def run():
        svc._ocr_semaphore = None

        @svc._ocr_limited
        async def engine():
            return "ok"

        # 3 sequential runs — if release were skipped, the 2nd call would hang.
        for _ in range(3):
            assert await asyncio.wait_for(engine(), timeout=2) == "ok"

    try:
        asyncio.run(run())
    finally:
        svc._ocr_semaphore = None


# ─── 2. MinerU budget caps subprocess timeout env ────────────────────────────

def test_mineru_budget_caps_subprocess_timeout(monkeypatch, tmp_path):
    import app.services.local_ocr_service as svc
    import app.benchmark.engines.mineru_engine as me
    import app.services.k12_batch.pipeline as kp

    class FakeResult:
        status = "success"
        markdown = "hello mineru"
        error = None

    class FakeEngine:
        def is_available(self):
            return True

        def run(self, file_path, work_dir):
            return FakeResult()

    monkeypatch.setattr(me, "MinerUEngine", FakeEngine)
    monkeypatch.setattr(kp, "read_content_list", lambda wd: [])
    monkeypatch.setenv("MINERU_TIMEOUT_SECONDS", "1800")

    async def run():
        svc._ocr_semaphore = None
        return await svc._run_mineru_pipeline(
            str(tmp_path / "dummy.pdf"), "f" * 32, budget_seconds=300
        )

    try:
        out = asyncio.run(run())
        assert out["text"] == "hello mineru"
        # Budget (300s) < MINERU_TIMEOUT_SECONDS (1800s) → env capped to budget
        # so the CLI subprocess self-kills when the parser would give up anyway.
        assert os.environ["OCR_BENCHMARK_PER_ENGINE_TIMEOUT"] == "300"
        assert os.environ["MINERU_BENCHMARK_TIMEOUT_SECONDS"] == "300"
    finally:
        svc._ocr_semaphore = None
        os.environ.pop("OCR_BENCHMARK_PER_ENGINE_TIMEOUT", None)
        os.environ.pop("MINERU_BENCHMARK_TIMEOUT_SECONDS", None)


def test_mineru_no_budget_keeps_configured_timeout(monkeypatch, tmp_path):
    import app.services.local_ocr_service as svc
    import app.benchmark.engines.mineru_engine as me
    import app.services.k12_batch.pipeline as kp

    class FakeResult:
        status = "success"
        markdown = "x"
        error = None

    class FakeEngine:
        def is_available(self):
            return True

        def run(self, file_path, work_dir):
            return FakeResult()

    monkeypatch.setattr(me, "MinerUEngine", FakeEngine)
    monkeypatch.setattr(kp, "read_content_list", lambda wd: [])
    monkeypatch.setenv("MINERU_TIMEOUT_SECONDS", "1234")

    async def run():
        svc._ocr_semaphore = None
        return await svc._run_mineru_pipeline(str(tmp_path / "d.pdf"), "e" * 32)

    try:
        asyncio.run(run())
        assert os.environ["OCR_BENCHMARK_PER_ENGINE_TIMEOUT"] == "1234"
    finally:
        svc._ocr_semaphore = None
        os.environ.pop("OCR_BENCHMARK_PER_ENGINE_TIMEOUT", None)
        os.environ.pop("MINERU_BENCHMARK_TIMEOUT_SECONDS", None)


# ─── 3. Orphan exam recovery at boot ─────────────────────────────────────────

def test_orphan_exam_recovery_on_boot(client):
    # Seed exams stuck mid-processing directly in the test DB (sync sqlite3 —
    # avoids event-loop juggling; conftest forces DATABASE_URL to _pytest.db).
    conn = sqlite3.connect("_pytest.db")
    ids = {}
    for status in ("pending", "processing", "completed", "needs_review"):
        cur = conn.execute(
            "INSERT INTO exam (user_id, filename, status) VALUES (1, ?, ?)",
            (f"stuck_{status}.pdf", status),
        )
        ids[status] = cur.lastrowid
    conn.commit()
    conn.close()

    # Re-enter the app lifespan → orphan recovery runs (startup is idempotent).
    from starlette.testclient import TestClient
    from app.main import app

    with TestClient(app):
        pass

    conn = sqlite3.connect("_pytest.db")
    rows = {
        s: conn.execute(
            "SELECT status, error_message FROM exam WHERE id=?", (i,)
        ).fetchone()
        for s, i in ids.items()
    }
    conn.close()

    assert rows["pending"][0] == "failed"
    assert rows["processing"][0] == "failed"
    assert "khởi động lại" in (rows["processing"][1] or "")
    # Trạng thái chờ user / đã xong không bị đụng.
    assert rows["completed"][0] == "completed"
    assert rows["needs_review"][0] == "needs_review"
