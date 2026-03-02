/**
 * generator.js — AI question generation + save to bank + export (gen & bank)
 */

/* ===== Exam config presets ===== */
const EXAM_TYPE_PRESETS = {
  kt15:    { countTN: 5,  countTL: 1,  label: 'Kiểm tra 15 phút' },
  kt1tiet: { countTN: 28, countTL: 2,  label: 'Kiểm tra 1 tiết' },
  giuaky:  { countTN: 28, countTL: 2,  label: 'Giữa kỳ' },
  cuoiky:  { countTN: 28, countTL: 3,  label: 'Cuối kỳ' },
  thpt:    { countTN: 50, countTL: 0,  label: 'THPT Quốc gia' },
  custom:  { countTN: null, countTL: null, label: 'Tùy chọn' }
};

const DIFF_PRESETS = {
  balanced: { NB: 40, TH: 30, VD: 20, VDC: 10 },
  easy:     { NB: 50, TH: 35, VD: 15, VDC: 0  },
  hard:     { NB: 25, TH: 30, VD: 30, VDC: 15 },
  hsg:      { NB: 10, TH: 20, VD: 40, VDC: 30 }
};

let _currentExamType = 'giuaky';
let _currentDiffPreset = 'balanced';
let _currentScope = 'chapter';

window.selectExamType = function(btn, type) {
  document.querySelectorAll('.gen-exam-type-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _currentExamType = type;
  const preset = EXAM_TYPE_PRESETS[type];
  const cfg = $('genCustomConfig');
  if (type === 'custom') {
    cfg.style.display = 'block';
  } else {
    cfg.style.display = 'none';
    if ($('genCountTN')) $('genCountTN').value = preset.countTN;
    if ($('genCountTL')) $('genCountTL').value = preset.countTL;
  }
};

window.selectDiff = function(btn, diff) {
  document.querySelectorAll('.gen-diff-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _currentDiffPreset = diff;
  if ($('genDiff')) $('genDiff').value = diff;
};

window.selectScope = function(btn, scope) {
  document.querySelectorAll('.gen-scope-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _currentScope = scope;
  if ($('genScope')) $('genScope').value = scope;
  // Ẩn/hiện cascade chương khi chuyển phạm vi
  const chGroup = $('genChapterGroup');
  const lsGroup = $('genLessonGroup');
  if (chGroup) chGroup.style.display = scope === 'chapter' ? '' : 'none';
  if (lsGroup) lsGroup.style.display = 'none';
  updateBankInfo();
};

/* ===== Generator filters ===== */
async function loadGenFilters() {
    try {
        const res = await fetch('/api/v1/questions/filters', { headers: authHeaders });
        if (!res.ok) return;
        const f = await res.json();
        populateSelect($('genType'), f.types, 'Dạng bất kỳ');
        const topicEl = $('genTopic');
        const currentTopic = topicEl.value;
        topicEl.innerHTML = '<option value="">Chủ đề bất kỳ</option>' +
            f.topics.map(t => `<option value="${esc(t)}" ${t === currentTopic ? 'selected' : ''}>${esc(t)}</option>`).join('');
        const hint = $('genHint');
        if (f.total_questions > 0) {
            hint.textContent = `Ngân hàng hiện có ${f.total_questions} câu — AI sẽ tham khảo để sinh câu tương tự`;
        } else {
            hint.textContent = 'Chưa có câu mẫu. Upload đề thi trước để AI sinh câu chính xác hơn.';
        }
    } catch (e) { console.error('Gen filters error', e); }
}

window.toggleGenMode = function() {
    const mode = $('genMode').value;
    $('singleModeFields').style.display = mode === 'single' ? '' : 'none';
    $('examModeFields').style.display = mode === 'exam' ? '' : 'none';
};

window.updateExamTotal = function() {
    const nb = parseInt($('distNB').value) || 0;
    const th = parseInt($('distTH').value) || 0;
    const vd = parseInt($('distVD').value) || 0;
    const vdc = parseInt($('distVDC').value) || 0;
    $('examTotal').textContent = `Tổng: ${nb + th + vd + vdc} câu`;
};

/* ===== Generate ===== */
window.doGenerate = async function() {
    const btn = $('genBtn');
    btn.classList.add('loading'); btn.disabled = true;
    $('genResults').classList.remove('visible');
    const mode = $('genMode').value;

    try {
        let url, body;
        if (mode === 'exam') {
            const sections = [];
            const nb = parseInt($('distNB').value) || 0;
            const th = parseInt($('distTH').value) || 0;
            const vd = parseInt($('distVD').value) || 0;
            const vdc = parseInt($('distVDC').value) || 0;
            if (nb > 0) sections.push({ difficulty: 'NB', count: nb });
            if (th > 0) sections.push({ difficulty: 'TH', count: th });
            if (vd > 0) sections.push({ difficulty: 'VD', count: vd });
            if (vdc > 0) sections.push({ difficulty: 'VDC', count: vdc });
            if (sections.length === 0) throw new Error('Chưa chọn số câu nào');
            url = '/api/v1/generate/exam';
            body = { topic: $('genTopic').value, question_type: $('genTypeExam').value, sections };
        } else {
            url = '/api/v1/generate';
            body = { question_type: $('genType').value, topic: $('genTopic').value, difficulty: $('genDiff').value, count: parseInt($('genCount').value) || 5 };
        }

        const res = await fetch(url, { method: 'POST', headers: { ...authHeaders, 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || 'Sinh đề thất bại'); }
        const data = await res.json();
        generatedResults = data.questions;
        displayGenerated(data);
        toast(`Đã sinh ${data.questions.length} câu hỏi mới!`, 'success');
    } catch (e) { toast(e.message, 'error'); }
    finally { btn.classList.remove('loading'); btn.disabled = false; }
};

function displayGenerated(data) {
    $('genResults').classList.add('visible');
    $('genResultsInfo').innerHTML = `<span>${data.questions.length}</span> câu đã sinh` +
        (data.sample_count > 0 ? ` (tham khảo ${data.sample_count} câu mẫu)` : '');

    const list = $('genResultsList');
    const isExam = $('genMode').value === 'exam';
    let html = '';

    if (isExam) {
        const groups = {};
        const order = ['NB', 'TH', 'VD', 'VDC'];
        data.questions.forEach(q => { const d = q.difficulty || 'TH'; if (!groups[d]) groups[d] = []; groups[d].push(q); });
        let num = 0;
        for (const diff of order) {
            if (!groups[diff] || !groups[diff].length) continue;
            const color = DIFF_COLORS[diff] || '#666';
            const label = DIFF_LABELS[diff] || diff;
            html += `<div style="margin-top:16px;padding:8px 14px;background:${color}11;border-left:4px solid ${color};border-radius:0 8px 8px 0;">
                <span style="font-weight:700;color:${color};font-size:0.92rem;">${label}</span>
                <span style="color:#666;font-size:0.82rem;margin-left:8px;">${groups[diff].length} câu</span>
            </div>`;
            for (const q of groups[diff]) { num++; html += renderGenCard(q, num); }
        }
    } else {
        data.questions.forEach((q, i) => { html += renderGenCard(q, i + 1); });
    }
    list.innerHTML = html;
    renderMath(list);
    refreshPreview();
}

function renderGenCard(q, num) {
    const steps = q.solution_steps || [];
    const color = DIFF_COLORS[q.difficulty] || '#7c3aed';
    return `<div class="gen-q-card" style="border-left-color:${color}">
        <div class="q-top">
            <span class="q-num">Câu ${num}</span>
            <div class="q-badges">
                <span class="gen-tag">AI</span>
                ${renderCurriculumBadges(q)}
            </div>
        </div>
        <div class="q-text">${esc(q.question || '')}</div>
        ${q.answer ? `<div class="q-answer"><div class="q-answer-label">Đáp án</div><div class="q-answer-text">${esc(q.answer)}</div></div>` : ''}
        ${steps.length ? `<div class="q-solution"><div class="q-solution-label">Lời giải</div><ul class="q-steps">${steps.map(s => '<li>' + esc(s) + '</li>').join('')}</ul></div>` : ''}
    </div>`;
}

/* ===== Download / Save to bank ===== */
window.downloadGenerated = function() {
    if (!generatedResults) return;
    const blob = new Blob([JSON.stringify(generatedResults, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'generated_questions_' + Date.now() + '.json'; a.click(); URL.revokeObjectURL(url);
};

window.saveGenToBank = async function() {
    if (!generatedResults || !generatedResults.length) { toast('Chưa có câu hỏi để lưu', 'error'); return; }
    const btn = $('btnSaveToBank');
    btn.disabled = true; btn.textContent = '⏳ Đang lưu...';
    try {
        const questions = generatedResults.map(q => ({
            question_text: q.question || '', question_type: q.type || 'TN', topic: q.topic || '',
            difficulty: q.difficulty || 'TH', grade: q.grade || null, chapter: q.chapter || '',
            lesson_title: q.lesson_title || '', answer: q.answer || '', solution_steps: JSON.stringify(q.solution_steps || []),
        }));
        const res = await fetch('/api/v1/questions/bulk', { method: 'POST', headers: { ...authHeaders, 'Content-Type': 'application/json' }, body: JSON.stringify({ questions }) });
        if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || 'Lưu thất bại'); }
        const data = await res.json();
        toast(`✅ ${data.detail}`, 'success');
        btn.textContent = '✅ Đã lưu!'; btn.style.background = '#059669';
        loadBankFilters();
    } catch (e) { toast(e.message, 'error'); btn.disabled = false; btn.textContent = '💾 Lưu vào ngân hàng'; btn.style.background = '#16a34a'; }
};

/* ===== Export ===== */
window.toggleExportMenu = function(menuId) {
    const menu = $(menuId);
    const wasVisible = menu.classList.contains('visible');
    document.querySelectorAll('.export-menu').forEach(m => m.classList.remove('visible'));
    if (!wasVisible) menu.classList.add('visible');
};
document.addEventListener('click', (e) => {
    if (!e.target.closest('.export-dropdown')) document.querySelectorAll('.export-menu').forEach(m => m.classList.remove('visible'));
});

function _getGenExportPayload() {
    if (!generatedResults || !generatedResults.length) { toast('Chưa có câu hỏi để xuất', 'error'); return null; }
    const isExam = $('genMode').value === 'exam';
    const topic = $('genTopic').value || '';
    const type = isExam ? 'Kiểm tra tổng hợp' : ($('genType') ? $('genType').value : 'Toán');
    return {
        questions: generatedResults.map(q => ({ question: q.question || '', type: q.type || 'TN', topic: q.topic || '', difficulty: q.difficulty || 'TH', grade: q.grade || null, chapter: q.chapter || '', lesson_title: q.lesson_title || '', answer: q.answer || '', solution_steps: q.solution_steps || [] })),
        title: isExam ? 'ĐỀ KIỂM TRA' : 'ĐỀ THI TOÁN HỌC', subtitle: topic || type,
        include_answers: true, include_solutions: true, group_by_diff: isExam,
    };
}

async function _doExport(url, payload, isPdf) {
    const res = await fetch(url, { method: 'POST', headers: { ...authHeaders, 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || 'Export failed: ' + res.status); }
    if (isPdf) {
        const html = await res.text();
        const w = window.open('', '_blank');
        if (w) { w.document.write(html); w.document.close(); } else toast('Trình duyệt chặn popup.', 'error');
    } else {
        const blob = await res.blob();
        const cd = res.headers.get('Content-Disposition') || '';
        const fnMatch = cd.match(/filename="?([^"]+)"?/);
        const filename = fnMatch ? fnMatch[1] : 'export_' + Date.now();
        const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = filename; a.click(); URL.revokeObjectURL(a.href);
        toast('Đã tải file ' + filename, 'success');
    }
}

window.exportGenAs = async function(format) {
    document.querySelectorAll('.export-menu').forEach(m => m.classList.remove('visible'));
    const payload = _getGenExportPayload();
    if (!payload) return;
    toast('Đang tạo file...', 'info');
    try { await _doExport('/api/v1/export/' + format, payload, format === 'pdf'); }
    catch (e) { console.error('Export error:', e); toast('Lỗi xuất file: ' + e.message, 'error'); }
};

window.exportBankAs = async function(format) {
    document.querySelectorAll('.export-menu').forEach(m => m.classList.remove('visible'));
    const payload = {
        topic: $('filterTopic') ? $('filterTopic').value : '', difficulty: $('filterDiff') ? $('filterDiff').value : '',
        question_type: $('filterType') ? $('filterType').value : '', keyword: $('filterKeyword') ? $('filterKeyword').value : '',
        limit: 200, title: 'NGÂN HÀNG CÂU HỎI', subtitle: ($('filterTopic') ? $('filterTopic').value : '') || 'Toán học',
        include_answers: true, include_solutions: true, group_by_diff: true,
    };
    toast('Đang tạo file...', 'info');
    try {
        const endpoint = '/api/v1/export/bank/' + (format === 'docx-split' ? 'docx' : format);
        await _doExport(endpoint, payload, format === 'pdf');
    } catch (e) { console.error('Bank export error:', e); toast(e.message || 'Lỗi xuất file', 'error'); }
};

window.exportPDF = function() { exportGenAs('pdf'); };

/* ===== PDF Preview ===== */
window.refreshPreview = async function() {
    if (!generatedResults || !generatedResults.length) return;
    const payload = _getGenExportPayload();
    if (!payload) return;

    payload.include_answers = $('previewAnswers').checked;
    payload.include_solutions = $('previewSolutions').checked;

    try {
        const res = await fetch('/api/v1/export/pdf', {
            method: 'POST',
            headers: { ...authHeaders, 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!res.ok) return;
        const html = await res.text();

        // Remove print toolbar and adjust for inline preview
        const cleanHtml = html
            .replace(/<div class="print-toolbar">[\s\S]*?<\/div>/, '')
            .replace('</style>', '\n.exam-container{margin:0 auto;padding:24px 28px;}body{background:#fff;}\n</style>');

        // Show panel + iframe
        $('genPreviewPanel').style.display = 'flex';
        const frame = $('genPreviewFrame');
        frame.style.display = 'block';
        frame.srcdoc = cleanHtml;
    } catch (e) {
        console.error('Preview error:', e);
    }
};

window.printPreview = function() {
    const frame = $('genPreviewFrame');
    if (frame && frame.contentWindow) {
        frame.contentWindow.focus();
        frame.contentWindow.print();
    }
};

/* ═══════════════════════════════════════════════════════════════
   CURRICULUM CASCADE — Lớp → Chương → Bài → Đếm câu
   ═══════════════════════════════════════════════════════════════ */

/** Cache để tránh gọi API lặp lại */
const _curriculumCache = {};
let _curriculumTree = null;   // full tree loaded once

/** Load cây chương trình một lần duy nhất */
async function ensureCurriculumTree() {
    if (_curriculumTree) return _curriculumTree;
    try {
        const token = localStorage.getItem('token');
        const res = await fetch('/api/v1/curriculum/tree', {
            headers: { 'Authorization': 'Bearer ' + token }
        });
        if (!res.ok) throw new Error('Cannot load curriculum');
        _curriculumTree = await res.json();
        return _curriculumTree;
    } catch (e) {
        console.warn('Curriculum load failed:', e);
        return null;
    }
}

/** Khi giáo viên click chọn lớp */
window.selectGrade = async function(btn, grade) {
    // Update pill UI
    document.querySelectorAll('.gen-grade-pill').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $('genGrade').value = grade;

    // Reset chapter/lesson
    $('genChapter').innerHTML = '<option value="">Tất cả chương</option>';
    $('genLesson').innerHTML  = '<option value="">Tất cả bài</option>';
    $('genLessonGroup').style.display = 'none';

    // Hiện/ẩn scope group
    const scopeGroup = $('genScopeGroup');
    if (scopeGroup) scopeGroup.style.display = grade ? '' : 'none';

    if (!grade) {
        $('genChapterGroup').style.display = 'none';
        updateBankInfo();
        return;
    }

    // Nếu scope không phải 'chapter' → không load cascade
    if (_currentScope !== 'chapter') {
        updateBankInfo(null, grade, null, null);
        return;
    }

    // Show chapter group with spinner label
    $('genChapterGroup').style.display = '';
    $('genChapterCount').textContent = '...';

    const tree = await ensureCurriculumTree();
    if (!tree) { $('genChapterCount').textContent = ''; return; }

    const gradeNode = tree.grades.find(g => g.grade == grade);
    if (!gradeNode) { $('genChapterCount').textContent = ''; return; }

    // Populate chapter select
    const chapterSel = $('genChapter');
    chapterSel.innerHTML = '<option value="">Tất cả chương</option>' +
        gradeNode.chapters.map(ch => {
            const qLabel = ch.question_count > 0 ? ` (${ch.question_count} câu)` : '';
            // Shorten chapter title: "Chương I. Tên dài..." → "Ch.I – Tên dài..."
            const short = ch.chapter.replace(/^Chương\s+/, 'Ch.').replace(/\.\s+/, ' – ');
            return `<option value="${ch.chapter_no}" data-chapter="${esc(ch.chapter)}">${esc(short)}${qLabel}</option>`;
        }).join('');

    // Total count for this grade
    $('genChapterCount').textContent = gradeNode.question_count > 0
        ? `${gradeNode.question_count} câu`
        : 'Chưa có câu';

    updateBankInfo(gradeNode.question_count, grade, null, null);
};

/** Khi chọn chương */
window.onChapterChange = async function() {
    const grade = $('genGrade').value;
    const chapterNo = $('genChapter').value;

    $('genLesson').innerHTML = '<option value="">Tất cả bài</option>';
    $('genLessonGroup').style.display = 'none';

    if (!chapterNo) {
        const tree = await ensureCurriculumTree();
        const gradeNode = tree?.grades.find(g => g.grade == grade);
        updateBankInfo(gradeNode?.question_count, grade, null, null);
        return;
    }

    const tree = await ensureCurriculumTree();
    if (!tree) return;
    const gradeNode = tree.grades.find(g => g.grade == grade);
    const chNode = gradeNode?.chapters.find(c => c.chapter_no == chapterNo);
    if (!chNode) return;

    // Populate lesson select
    const lessonSel = $('genLesson');
    lessonSel.innerHTML = '<option value="">Tất cả bài trong chương</option>' +
        chNode.lessons.map(ls => {
            const qLabel = ls.question_count > 0 ? ` (${ls.question_count} câu)` : '';
            return `<option value="${ls.id}" data-title="${esc(ls.lesson_title)}">${esc(ls.lesson_title)}${qLabel}</option>`;
        }).join('');

    $('genLessonGroup').style.display = '';
    $('genLessonCount').textContent = chNode.question_count > 0
        ? `${chNode.question_count} câu`
        : 'Chưa có câu';

    // Sync topic textarea với tên chương để AI hiểu ngữ cảnh
    const chapterName = chNode.chapter.replace(/^Chương\s+[IVXLC\d]+\.\s*/i, '');
    if (!$('genTopic').value.trim()) {
        $('genTopic').value = chapterName;
    }

    updateBankInfo(chNode.question_count, grade, chapterNo, null);
};

/** Khi chọn bài cụ thể */
window.onLessonChange = async function() {
    const grade = $('genGrade').value;
    const chapterNo = $('genChapter').value;
    const lessonSel = $('genLesson');
    const lessonId = lessonSel.value;

    const tree = await ensureCurriculumTree();
    const gradeNode = tree?.grades.find(g => g.grade == grade);
    const chNode = gradeNode?.chapters.find(c => c.chapter_no == chapterNo);
    const lsNode = chNode?.lessons.find(l => l.id == lessonId);

    // Auto-fill topic textarea with lesson title
    if (lsNode) {
        $('genTopic').value = lsNode.lesson_title;
        updateBankInfo(lsNode.question_count, grade, chapterNo, lessonId);
    } else {
        updateBankInfo(chNode?.question_count, grade, chapterNo, null);
    }
};

/** Cập nhật thanh thông tin ngân hàng */
function updateBankInfo(count, grade, chapterNo, lessonId) {
    const bar = $('genBankInfo');
    const txt = $('genBankInfoText');
    if (!bar || !txt) return;

    if (grade === undefined || grade === null || grade === '') {
        bar.style.display = 'none'; return;
    }

    bar.style.display = 'flex';
    bar.classList.remove('has-data', 'no-data');

    const n = count || 0;
    if (n > 0) {
        bar.classList.add('has-data');
        txt.textContent = `${n} câu trong ngân hàng — AI sẽ tham khảo`;
    } else {
        bar.classList.add('no-data');
        txt.textContent = 'Chưa có câu trong ngân hàng — AI sẽ sinh từ đầu';
    }
}

/** generateQuestions — called by button onclick, wraps doGenerate with curriculum context */
window.generateQuestions = async function() {
    const grade = $('genGrade').value;
    const chapterNo = $('genChapter')?.value;
    const lessonSel = $('genLesson');
    const lessonTitle = lessonSel?.options[lessonSel.selectedIndex]?.dataset?.title || '';

    // Build context-rich topic for AI if not manually typed
    let topicText = $('genTopic').value.trim();
    if (!topicText && lessonTitle) topicText = lessonTitle;
    if (!topicText && chapterNo && grade) {
        const tree = _curriculumTree;
        const gradeNode = tree?.grades.find(g => g.grade == grade);
        const chNode = gradeNode?.chapters.find(c => c.chapter_no == chapterNo);
        if (chNode) topicText = chNode.chapter.replace(/^Chương\s+[IVXLC\d]+\.\s*/i, '');
    }
    if (topicText) $('genTopic').value = topicText;

    // Inject new config params into doGenerate
    const preset = EXAM_TYPE_PRESETS[_currentExamType] || EXAM_TYPE_PRESETS.giuaky;
    const countTN = _currentExamType === 'custom'
        ? (parseInt($('genCountTN')?.value) || 28)
        : preset.countTN;
    const countTL = _currentExamType === 'custom'
        ? (parseInt($('genCountTL')?.value) || 2)
        : preset.countTL;
    const totalCount = (countTN || 0) + (countTL || 0);

    // Patch genCount for doGenerate legacy call
    if ($('genCount')) $('genCount').value = totalCount;

    // Build extended context string for AI prompt
    const scopeLabels = { chapter: 'theo chương', hk1: 'Học kỳ I', hk2: 'Học kỳ II', full: 'cả năm' };
    const diffLabels = { balanced: 'cân bằng (40–30–20–10)', easy: 'dễ (50–35–15–0)', hard: 'khó (25–30–30–15)', hsg: 'HSG (10–20–40–30)' };
    const targetLabels = { dattra: 'đại trà', kha: 'khá–giỏi', hsg: 'học sinh giỏi/thi chuyên' };
    const target = $('genTarget')?.value || 'dattra';

    const contextParts = [];
    if (grade) contextParts.push(`Lớp ${grade}`);
    if (_currentScope !== 'chapter') contextParts.push(`Phạm vi: ${scopeLabels[_currentScope]}`);
    contextParts.push(`Loại đề: ${preset.label || _currentExamType}`);
    contextParts.push(`${countTN} câu trắc nghiệm, ${countTL} câu tự luận`);
    contextParts.push(`Độ khó ${diffLabels[_currentDiffPreset] || _currentDiffPreset}`);
    contextParts.push(`Đối tượng: ${targetLabels[target] || target}`);
    if (topicText) contextParts.push(`Nội dung: ${topicText}`);

    // Temporarily enrich genTopic with full context for AI
    const origTopic = $('genTopic').value;
    $('genTopic').value = contextParts.join(' · ');

    await window.doGenerate();

    // Restore user-typed topic
    $('genTopic').value = origTopic;
};

/* Override loadGenFilters to reset cache on tab switch */
const _origLoadGenFilters = window.loadGenFilters;
window.loadGenFilters = async function() {
    _curriculumTree = null;
    if (_origLoadGenFilters) await _origLoadGenFilters();
    ensureCurriculumTree().catch(() => {});
};