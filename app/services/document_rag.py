"""Document RAG — Hybrid document chunk + question embedding search & generation.

This module provides the enhanced RAG pipeline that combines:
1. document_chunk table — full document content (sections, examples, explanations)
2. question_embedding table — individual questions with answers (existing)

When generating an exam, both sources are searched and merged into a rich
context that gives Gemini both background knowledge AND example questions.

Tables:
    - document_chunk: stores embedded document chunks (pgvector)
    - question_embedding: stores embedded questions (existing, unchanged)

Dependencies:
    - app.services.vector_search: reuses _generate_embedding, _is_postgres
    - app.services.rag_generator: reuses _parse_prompt_to_criteria
"""

import json
import asyncio
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 768  # Same as question_embedding


# ========== TABLE INIT ==========

async def ensure_document_chunk_table(engine: AsyncEngine) -> None:
    """Create document_chunk table — called once on startup."""
    from app.services.vector_search import _is_postgres

    is_pg = _is_postgres()

    async with engine.begin() as conn:
        if is_pg:
            # PostgreSQL with pgvector
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS document_chunk (
                    id SERIAL PRIMARY KEY,
                    exam_id INTEGER REFERENCES exam(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    section_title TEXT,
                    page_number INTEGER,
                    subject_code VARCHAR(30),
                    grade INTEGER,
                    metadata_json TEXT,
                    embedding vector({EMBEDDING_DIM}) NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))

            # Indexes
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_docchunk_user "
                "ON document_chunk(user_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_docchunk_exam "
                "ON document_chunk(exam_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_docchunk_subject_grade "
                "ON document_chunk(user_id, subject_code, grade)"
            ))

            # HNSW index — only if enough rows (avoid overhead on empty table)
            try:
                count = (await conn.execute(
                    text("SELECT COUNT(*) FROM document_chunk")
                )).scalar() or 0
                if count >= 50:
                    await conn.execute(text(f"""
                        CREATE INDEX IF NOT EXISTS ix_docchunk_hnsw
                        ON document_chunk
                        USING hnsw (embedding vector_cosine_ops)
                        WITH (m = 16, ef_construction = 64)
                    """))
                    logger.info(f"document_chunk HNSW index ready ({count} chunks)")
            except Exception as e:
                logger.debug(f"HNSW index skipped for document_chunk: {e}")

        else:
            # SQLite fallback
            await conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS document_chunk (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exam_id INTEGER REFERENCES exam(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    section_title TEXT,
                    page_number INTEGER,
                    subject_code VARCHAR(30),
                    grade INTEGER,
                    metadata_json TEXT,
                    embedding TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_docchunk_user "
                "ON document_chunk(user_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_docchunk_exam "
                "ON document_chunk(exam_id)"
            ))

    logger.info("document_chunk table initialized")


# ========== EMBED CHUNKS ==========

async def embed_document_chunks(
    db: AsyncSession,
    exam_id: int,
    chunks: list[dict],
    user_id: int,
    subject_code: str = None,
    grade: int = None,
) -> int:
    """Embed document chunks and store in pgvector.

    Reuses existing embedding infrastructure from vector_search.py.

    Args:
        db: Database session.
        exam_id: FK to exam table (document upload).
        chunks: List of chunk dicts from docling_chunker.
        user_id: Owner user ID.
        subject_code: Subject code for filtering.
        grade: Grade for filtering.

    Returns:
        Number of chunks successfully embedded.
    """
    from app.services.vector_search import _generate_embedding, _is_postgres

    if not chunks:
        return 0

    is_pg = _is_postgres()
    stored = 0

    # Process in batches of 10 to avoid overwhelming the embedding API
    batch_size = 10
    for batch_start in range(0, len(chunks), batch_size):
        batch = chunks[batch_start:batch_start + batch_size]

        # Generate embeddings in parallel
        texts = []
        for chunk in batch:
            # Enrich text with context for better embedding
            prefix_parts = []
            if subject_code:
                prefix_parts.append(subject_code)
            if grade:
                prefix_parts.append(f"lớp {grade}")
            if chunk.get("section_title"):
                prefix_parts.append(chunk["section_title"])

            prefix = " | ".join(prefix_parts)
            text_body = chunk["text"][:1500]  # Cap at 1500 chars for embedding
            enriched = f"{prefix}: {text_body}" if prefix else text_body
            texts.append(enriched)

        # Generate embeddings
        embeddings = await asyncio.gather(
            *[_generate_embedding(t) for t in texts]
        )

        # Store each chunk
        for chunk, emb in zip(batch, embeddings):
            if emb is None:
                continue

            metadata = {
                "heading_trail": chunk.get("heading_trail", ""),
                "char_count": chunk.get("char_count", 0),
            }

            params = {
                "exam_id": exam_id,
                "uid": user_id,
                "chunk_text": chunk["text"][:5000],  # Cap storage
                "chunk_index": chunk["index"],
                "section_title": chunk.get("section_title", ""),
                "page_number": chunk.get("page"),
                "subject_code": subject_code,
                "grade": grade,
                "metadata_json": json.dumps(metadata, ensure_ascii=False),
                "emb": str(emb) if is_pg else json.dumps(emb),
            }

            try:
                if is_pg:
                    await db.execute(text("""
                        INSERT INTO document_chunk
                        (exam_id, user_id, chunk_text, chunk_index,
                         section_title, page_number, subject_code, grade,
                         metadata_json, embedding)
                        VALUES (:exam_id, :uid, :chunk_text, :chunk_index,
                                :section_title, :page_number, :subject_code, :grade,
                                :metadata_json, :emb::vector)
                    """), params)
                else:
                    await db.execute(text("""
                        INSERT INTO document_chunk
                        (exam_id, user_id, chunk_text, chunk_index,
                         section_title, page_number, subject_code, grade,
                         metadata_json, embedding)
                        VALUES (:exam_id, :uid, :chunk_text, :chunk_index,
                                :section_title, :page_number, :subject_code, :grade,
                                :metadata_json, :emb)
                    """), params)
                stored += 1
            except Exception as e:
                logger.warning(f"Chunk insert failed: {e}")

    try:
        await db.commit()
    except Exception as e:
        logger.warning(f"Chunk batch commit failed: {e}")
        await db.rollback()

    logger.info(f"Embedded {stored}/{len(chunks)} document chunks for exam {exam_id}")
    return stored


async def delete_document_chunks(db: AsyncSession, exam_id: int) -> int:
    """Xoá toàn bộ document chunks của 1 exam (để re-index từ markdown đã sửa)."""
    try:
        result = await db.execute(
            text("DELETE FROM document_chunk WHERE exam_id = :eid"),
            {"eid": exam_id},
        )
        await db.commit()
        return int(getattr(result, "rowcount", 0) or 0)
    except Exception as e:
        logger.warning(f"delete_document_chunks failed for exam {exam_id}: {e}")
        await db.rollback()
        return 0


# ========== HYBRID SEARCH ==========

async def hybrid_search(
    db: AsyncSession,
    query: str,
    user_id: int,
    subject_code: str = None,
    grade: int = None,
    doc_limit: int = 8,
    q_limit: int = 5,
    min_similarity: float = 0.25,
) -> dict:
    """Search both document_chunk AND question_embedding tables.

    Returns merged context from both sources, ranked by relevance.

    Args:
        db: Database session.
        query: User's natural language query.
        user_id: Owner user ID.
        subject_code: Filter by subject.
        grade: Filter by grade.
        doc_limit: Max document chunks to return.
        q_limit: Max similar questions to return.
        min_similarity: Minimum cosine similarity threshold.

    Returns:
        {
            "doc_chunks": [{"text": ..., "section_title": ..., "score": ...}],
            "similar_questions": [{"question_text": ..., "answer": ..., "score": ...}],
        }
    """
    from app.services.vector_search import (
        _generate_embedding, _is_postgres, enrich_text_for_embedding,
    )

    # Generate query embedding
    enriched_query = enrich_text_for_embedding(
        query, topic=subject_code or "", grade=grade,
    )
    query_emb = await _generate_embedding(enriched_query)
    if query_emb is None:
        logger.warning("Failed to generate query embedding for hybrid search")
        return {"doc_chunks": [], "similar_questions": []}

    is_pg = _is_postgres()

    # Run both searches in parallel
    doc_task = _search_document_chunks(
        db, query_emb, user_id, subject_code, grade, doc_limit, min_similarity, is_pg,
    )
    q_task = _search_question_embeddings(
        db, query_emb, user_id, subject_code, grade, q_limit, min_similarity, is_pg,
    )

    doc_chunks, similar_questions = await asyncio.gather(doc_task, q_task)

    logger.info(
        f"Hybrid search: {len(doc_chunks)} doc chunks + "
        f"{len(similar_questions)} similar questions"
    )

    return {
        "doc_chunks": doc_chunks,
        "similar_questions": similar_questions,
    }


async def _search_document_chunks(
    db: AsyncSession,
    query_emb: list[float],
    user_id: int,
    subject_code: str = None,
    grade: int = None,
    limit: int = 8,
    min_similarity: float = 0.25,
    is_pg: bool = True,
) -> list[dict]:
    """Search document_chunk table using pgvector or numpy fallback."""

    # Check if table has data
    try:
        conditions = ["user_id = :uid"]
        params = {"uid": user_id}
        if subject_code:
            conditions.append("subject_code = :subj")
            params["subj"] = subject_code
        if grade:
            conditions.append("(grade = :grade OR grade IS NULL)")
            params["grade"] = grade

        where = " AND ".join(conditions)
        count = (await db.execute(
            text(f"SELECT COUNT(*) FROM document_chunk WHERE {where}"), params
        )).scalar() or 0

        if count == 0:
            return []
    except Exception:
        return []  # Table might not exist yet

    if is_pg:
        return await _search_chunks_pgvector(
            db, query_emb, where, params, limit, min_similarity,
        )
    else:
        return await _search_chunks_numpy(
            db, query_emb, where, params, limit, min_similarity,
        )


async def _search_chunks_pgvector(
    db, query_emb, where_clause, params, limit, min_similarity,
) -> list[dict]:
    """pgvector: cosine distance search on document_chunk."""
    max_distance = 1.0 - min_similarity
    params["emb"] = str(query_emb)
    params["max_dist"] = max_distance
    params["lim"] = limit

    result = await db.execute(text(f"""
        SELECT
            chunk_text,
            section_title,
            page_number,
            grade,
            subject_code,
            1 - (embedding <=> :emb::vector) AS similarity
        FROM document_chunk
        WHERE {where_clause}
          AND (embedding <=> :emb::vector) <= :max_dist
        ORDER BY embedding <=> :emb::vector
        LIMIT :lim
    """), params)

    return [
        {
            "text": row[0],
            "section_title": row[1] or "",
            "page": row[2],
            "grade": row[3],
            "subject_code": row[4],
            "score": float(row[5]),
        }
        for row in result.fetchall()
    ]


async def _search_chunks_numpy(
    db, query_emb, where_clause, params, limit, min_similarity,
) -> list[dict]:
    """SQLite fallback: load embeddings + numpy cosine similarity."""
    import numpy as np

    result = await db.execute(text(f"""
        SELECT chunk_text, section_title, page_number, grade, subject_code, embedding
        FROM document_chunk
        WHERE {where_clause}
    """), params)
    rows = result.fetchall()

    if not rows:
        return []

    query_vec = np.array(query_emb, dtype=np.float32)
    q_norm = np.linalg.norm(query_vec)
    if q_norm == 0:
        return []

    scored = []
    for row in rows:
        try:
            emb = json.loads(row[5]) if isinstance(row[5], str) else row[5]
            emb_vec = np.array(emb, dtype=np.float32)
            e_norm = np.linalg.norm(emb_vec)
            if e_norm == 0:
                continue
            sim = float(np.dot(query_vec, emb_vec) / (q_norm * e_norm))
            if sim >= min_similarity:
                scored.append({
                    "text": row[0],
                    "section_title": row[1] or "",
                    "page": row[2],
                    "grade": row[3],
                    "subject_code": row[4],
                    "score": sim,
                })
        except Exception:
            continue

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


async def _search_question_embeddings(
    db: AsyncSession,
    query_emb: list[float],
    user_id: int,
    subject_code: str = None,
    grade: int = None,
    limit: int = 5,
    min_similarity: float = 0.25,
    is_pg: bool = True,
) -> list[dict]:
    """Search existing question_embedding table (reuses vector_search logic)."""
    try:
        from app.services.vector_search import find_similar

        # Build a query string from the embedding context
        query_parts = []
        if subject_code:
            query_parts.append(subject_code)
        if grade:
            query_parts.append(f"lớp {grade}")
        query_str = " ".join(query_parts) if query_parts else "câu hỏi"

        similar = await find_similar(
            db, query_str, user_id,
            subject_code=subject_code,
            grade=grade,
            limit=limit,
            min_similarity=min_similarity,
        )

        if not similar:
            return []

        # Fetch full question data
        from app.db.models.question import Question
        from sqlalchemy import select

        ids = [s["question_id"] for s in similar]
        rows = (await db.execute(
            select(Question).where(Question.id.in_(ids))
        )).scalars().all()

        # Build similarity score map
        score_map = {s["question_id"]: s["similarity"] for s in similar}

        return [
            {
                "question_id": q.id,
                "question_text": q.question_text,
                "topic": q.topic or "",
                "difficulty": q.difficulty or "",
                "grade": q.grade,
                "answer": q.answer or "",
                "solution_steps": q.solution_steps or "[]",
                "score": score_map.get(q.id, 0),
            }
            for q in rows
        ]
    except Exception as e:
        logger.debug(f"Question embedding search failed: {e}")
        return []


# ========== RAG EXAM GENERATION ==========

_RAG_GENERATE_PROMPT = """Bạn là chuyên gia toán học Việt Nam. Sinh {count} câu hỏi MỚI.

TIÊU CHÍ:
- Dạng bài: {q_type}
- Chủ đề: {topic}
- Độ khó: {difficulty}
- Câu hỏi KHÁC số liệu so với câu mẫu nhưng GIỐNG dạng bài
- Đáp án CHÍNH XÁC, lời giải NGẮN GỌN (tối đa 3 bước)
- LaTeX: dùng $...$ và double backslash trong JSON (\\\\frac, \\\\sqrt)

KIẾN THỨC NỀN (từ sách giáo khoa/tài liệu đã upload):
{doc_context}

CÂU HỎI MẪU (từ ngân hàng câu hỏi):
{question_samples}

PHÂN LOẠI THEO CHƯƠNG TRÌNH GDPT 2018:
- grade: số nguyên 6-12
- chapter: Tên chương đầy đủ
- lesson_title: Tiêu đề bài học cụ thể

SINH {count} CÂU MỚI dựa trên kiến thức nền và câu mẫu trên.
Output: JSON array. Không markdown. Không giải thích."""


async def rag_generate_exam(
    db: AsyncSession,
    prompt: str,
    user_id: int,
    subject_code_override: str = None,
    grade_override: int = None,
    count_override: int = None,
    verify_answers: bool = True,
) -> dict:
    """Main entry point: hybrid RAG search → Gemini generate exam.

    Enhanced flow vs rag_generator.py:
    1. Parse prompt → criteria (reuse existing)
    2. Hybrid search: doc chunks + question embeddings
    3. Build rich context (doc knowledge + question examples)
    4. Gemini generate with context
    5. Verify answers + dedup

    Returns:
        {
            "questions": [...],
            "criteria": {...},
            "sample_count": int,
            "context_stats": {
                "doc_chunks_used": int,
                "questions_used": int,
            },
            "message": str,
            "verification": dict | None,
        }
    """
    from app.services.ai_generator import ai_generator
    from app.services.rag_generator import (
        _parse_prompt_to_criteria,
        _normalize_difficulty_mix,
    )

    if not ai_generator._client:
        raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình.")

    # ── Step 1: Parse prompt → criteria ──
    raw_criteria = await _parse_prompt_to_criteria(
        prompt, ai_generator._client, ai_generator.gemini_model, db
    )

    grade = grade_override or raw_criteria.get("grade")
    total = count_override or int(raw_criteria.get("total_count") or 10)
    total = max(1, min(50, total))
    chapters = raw_criteria.get("chapters") or []
    q_type = raw_criteria.get("question_type") or "TN"
    topic_hint = raw_criteria.get("topic_hint") or prompt[:100]
    diff_mix = _normalize_difficulty_mix(
        raw_criteria.get("difficulty_mix") or {}, total
    )

    logger.info(
        f"Document RAG criteria: grade={grade}, chapters={chapters}, "
        f"type={q_type}, mix={diff_mix}, total={total}"
    )

    # ── Step 2: Hybrid RAG search ──
    search_query = f"{topic_hint} {' '.join(chapters)}"
    subject_code = subject_code_override or raw_criteria.get("subject_code") or "toan"

    rag_results = await hybrid_search(
        db, search_query, user_id,
        subject_code=subject_code,
        grade=grade,
        doc_limit=10,
        q_limit=8,
    )

    doc_chunks = rag_results["doc_chunks"]
    similar_questions = rag_results["similar_questions"]

    # ── Step 3: Build context ──
    doc_context = _format_doc_context(doc_chunks)
    question_samples = _format_question_samples(similar_questions)

    # ── Step 4: Generate per difficulty section ──
    from google.genai import types
    from app.services.ai_parser import _SAFETY_SETTINGS

    gen_tasks = []
    task_labels = []

    for difficulty, count in diff_mix.items():
        if count <= 0:
            continue

        chapter_names = [c.split(".", 1)[-1] for c in chapters] if chapters else [topic_hint]
        section_topic = f"Toán {grade or ''} - {', '.join(chapter_names)}"

        gen_prompt = _RAG_GENERATE_PROMPT.format(
            count=count,
            q_type=q_type,
            topic=section_topic,
            difficulty=difficulty,
            doc_context=doc_context or "(Chưa có tài liệu tham khảo)",
            question_samples=question_samples or "(Chưa có câu mẫu)",
        )

        gen_tasks.append(_call_gemini_generate(
            ai_generator, gen_prompt, count,
        ))
        task_labels.append(f"{count}×{difficulty}")

    if not gen_tasks:
        raise RuntimeError("Không thể xác định cấu trúc đề từ yêu cầu.")

    logger.info(f"Generating sections: {', '.join(task_labels)}")
    gen_results = await asyncio.gather(*gen_tasks, return_exceptions=True)

    all_questions = []
    for i, res in enumerate(gen_results):
        if isinstance(res, Exception):
            logger.error(f"Section {task_labels[i]} failed: {res}")
        else:
            all_questions.extend(res)

    # ── Step 5: Answer verification ──
    verification = None
    if verify_answers and all_questions:
        try:
            from app.services.answer_verifier import answer_verifier
            verify_result = await answer_verifier.verify_and_fix(
                all_questions, auto_fix=True
            )
            all_questions = verify_result["questions"]
            verification = verify_result["stats"]
        except Exception as e:
            logger.warning(f"Answer verification skipped: {e}")

    # ── Step 6: Duplicate check ──
    try:
        from app.services.answer_verifier import answer_verifier
        all_questions = await answer_verifier.check_duplicates(
            db, all_questions, user_id, grade=grade
        )
    except Exception as e:
        logger.debug(f"Duplicate check skipped: {e}")

    # Build message
    chapters_str = ", ".join(c.split(".", 1)[-1] for c in chapters) if chapters else topic_hint
    mix_str = " + ".join(f"{v} {k}" for k, v in diff_mix.items())
    message = (
        f"Sinh {len(all_questions)}/{total} câu {q_type}"
        + (f" lớp {grade}" if grade else "")
        + (f" — {chapters_str}" if chapters_str else "")
        + f" ({mix_str})"
    )
    if doc_chunks:
        message += f" · RAG: {len(doc_chunks)} đoạn tài liệu + {len(similar_questions)} câu mẫu"
    elif similar_questions:
        message += f" · {len(similar_questions)} câu mẫu từ ngân hàng"
    else:
        message += " · không có tài liệu tham khảo"

    if verification:
        v = verification
        if v.get("fixed", 0) > 0 or v.get("removed", 0) > 0:
            message += f" · Kiểm tra: {v.get('fixed',0)} sửa, {v.get('removed',0)} loại"

    return {
        "questions": all_questions,
        "criteria": {
            "grade": grade,
            "chapters": chapters,
            "difficulty_mix": diff_mix,
            "question_type": q_type,
            "total_count": total,
            "topic_hint": topic_hint,
        },
        "sample_count": len(similar_questions),
        "context_stats": {
            "doc_chunks_used": len(doc_chunks),
            "questions_used": len(similar_questions),
        },
        "message": message,
        "verification": verification,
    }


# ========== HELPERS ==========

def _format_doc_context(chunks: list[dict], max_total_chars: int = 6000) -> str:
    """Format document chunks into a context block for the generation prompt."""
    if not chunks:
        return ""

    parts = []
    total = 0
    for i, chunk in enumerate(chunks, 1):
        section = chunk.get("section_title", "")
        text_content = chunk.get("text", "")
        score = chunk.get("score", 0)

        # Truncate if approaching limit
        remaining = max_total_chars - total
        if remaining <= 100:
            break
        text_content = text_content[:remaining]

        header = f"[Tài liệu {i}]"
        if section:
            header += f" ({section})"

        parts.append(f"{header}\n{text_content}")
        total += len(text_content) + len(header)

    return "\n\n".join(parts)


def _format_question_samples(questions: list[dict], max_samples: int = 5) -> str:
    """Format similar questions into sample text for the generation prompt."""
    if not questions:
        return ""

    parts = []
    for i, q in enumerate(questions[:max_samples], 1):
        text_content = q.get("question_text", "")[:500]
        answer = q.get("answer", "")
        difficulty = q.get("difficulty", "")
        topic = q.get("topic", "")

        line = f"Mẫu {i}"
        if topic:
            line += f" [{topic}]"
        if difficulty:
            line += f" ({difficulty})"
        line += f": {text_content}"
        if answer:
            line += f"\n  ĐA: {answer}"

        parts.append(line)

    return "\n".join(parts)


async def _call_gemini_generate(
    ai_generator,
    prompt: str,
    expected_count: int,
) -> list[dict]:
    """Call Gemini API for generation with 3-tier fallback."""
    from google.genai import types
    from app.services.ai_parser import _SAFETY_SETTINGS
    from app.services.ai_generator import QUESTION_SCHEMA

    for mime, schema, label in [
        ("application/json", QUESTION_SCHEMA, "Schema mode"),
        ("application/json", None, "JSON mode"),
        (None, None, "Plain text"),
    ]:
        try:
            cfg_kwargs = dict(
                temperature=0.7,
                max_output_tokens=32000,
                safety_settings=[types.SafetySetting(**s) for s in _SAFETY_SETTINGS],
            )
            if mime:
                cfg_kwargs["response_mime_type"] = mime
            if schema:
                cfg_kwargs["response_schema"] = schema

            for attempt in range(2):
                try:
                    response = await asyncio.wait_for(
                        ai_generator._client.aio.models.generate_content(
                            model=ai_generator.gemini_model,
                            contents=prompt,
                            config=types.GenerateContentConfig(**cfg_kwargs),
                        ),
                        timeout=90,
                    )
                    content = ai_generator._safe_text(response)
                    if content:
                        result = ai_generator._extract_json(content)
                        if result:
                            logger.info(f"RAG generate {label}: {len(result)} questions")
                            return result
                    break
                except asyncio.TimeoutError:
                    wait = (attempt + 1) * 5
                    logger.warning(f"RAG generate {label} timed out, retry in {wait}s")
                    await asyncio.sleep(wait)
                except Exception as e:
                    err = str(e)
                    if "429" in err or "RESOURCE_EXHAUSTED" in err:
                        wait = (attempt + 1) * 8
                        logger.warning(f"RAG generate rate limited, wait {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    logger.warning(f"RAG generate {label} failed: {e}")
                    break

        except Exception as e:
            logger.warning(f"RAG generate {label} outer error: {e}")

    logger.error("All RAG generate tiers failed")
    return []
