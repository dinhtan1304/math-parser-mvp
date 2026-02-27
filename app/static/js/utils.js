/**
 * utils.js — Shared utilities (load first)
 * Sprint 3, Task 17: Split monolithic HTML
 */

/* ===== Element shortcut ===== */
const $ = id => document.getElementById(id);

/* ===== HTML escape ===== */
function esc(t) {
    if (!t) return '';
    return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\n/g,'<br>').replace(/\n/g,'<br>');
}

/* ===== KaTeX render ===== */
function renderMath(el) {
    if (typeof renderMathInElement === 'function') {
        renderMathInElement(el, {
            delimiters: [
                { left: '$$', right: '$$', display: true },
                { left: '$', right: '$', display: false },
                { left: '\\(', right: '\\)', display: false },
                { left: '\\[', right: '\\]', display: true }
            ],
            throwOnError: false
        });
    }
}

/* ===== Format helpers ===== */
function fmtDate(d) {
    const dt = new Date(d);
    return dt.toLocaleDateString('vi-VN', { day:'2-digit', month:'2-digit' }) + ' ' +
           dt.toLocaleTimeString('vi-VN', { hour:'2-digit', minute:'2-digit' });
}

function fmtSize(b) {
    return b < 1024 ? b+' B' : b < 1048576 ? (b/1024).toFixed(1)+' KB' : (b/1048576).toFixed(1)+' MB';
}

/* ===== Toast notifications ===== */
function toast(msg, type = 'info') {
    const container = document.getElementById('toastContainer');
    if (!container) return;
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3500);
}

/* ===== Difficulty labels & colors ===== */
const DIFF_LABELS = { NB: 'Nhận biết', TH: 'Thông hiểu', VD: 'Vận dụng', VDC: 'Vận dụng cao' };
const DIFF_COLORS = { NB: '#16a34a', TH: '#2563eb', VD: '#d97706', VDC: '#dc2626' };

/* ===== Grade labels & colors ===== */
const GRADE_COLORS = {
    6: '#0891b2', 7: '#0d9488', 8: '#059669', 9: '#7c3aed',
    10: '#c026d3', 11: '#e11d48', 12: '#ea580c'
};

/* ===== Render curriculum badges ===== */
function renderCurriculumBadges(q) {
    const grade = q.grade || q.grade_level;
    const chapter = q.chapter || '';
    const lesson = q.lesson_title || '';
    const diff = q.difficulty || 'TH';
    const diffColor = DIFF_COLORS[diff] || '#7c3aed';
    const gradeColor = grade ? (GRADE_COLORS[grade] || '#6b7280') : '#6b7280';

    // Short chapter: "Chương I. Ứng dụng đạo hàm..." → "Chương I"
    let chapterShort = '';
    if (chapter) {
        const m = chapter.match(/Chương\s+[IVXLC\d]+/i);
        chapterShort = m ? m[0] : (chapter.length > 25 ? chapter.substring(0, 25) + '…' : chapter);
    }

    let html = '';

    // Grade badge
    if (grade) {
        html += `<span class="q-badge grade" style="background:${gradeColor}14;color:${gradeColor};border:1px solid ${gradeColor}30" title="Lớp ${grade}">Lớp ${grade}</span>`;
    }

    // Chapter badge
    if (chapterShort) {
        html += `<span class="q-badge chapter" title="${esc(chapter)}">${esc(chapterShort)}</span>`;
    }

    // Lesson title badge
    if (lesson) {
        html += `<span class="q-badge lesson" title="${esc(lesson)}">${esc(lesson.length > 35 ? lesson.substring(0, 35) + '…' : lesson)}</span>`;
    }

    // Fallback: if no grade/chapter/lesson yet (old data), show topic
    if (!grade && !chapter && !lesson) {
        const topic = q.topic || q.question_type || q.type || '';
        if (topic) {
            html += `<span class="q-badge chapter">${esc(topic.length > 35 ? topic.substring(0, 35) + '…' : topic)}</span>`;
        }
    }

    // Difficulty badge (always shown)
    html += `<span class="q-badge diff" style="background:${diffColor}18;color:${diffColor}">${esc(diff)}</span>`;
    return html;
}