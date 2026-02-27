/**
 * generator.js — AI question generation + save to bank + export (gen & bank)
 */

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
                <span class="q-badge type">${esc(q.type || '')}</span>
                <span class="q-badge diff" style="background:${color}18;color:${color}">${esc(q.difficulty || '')}</span>
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
            difficulty: q.difficulty || 'TH', answer: q.answer || '', solution_steps: JSON.stringify(q.solution_steps || []),
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
        questions: generatedResults.map(q => ({ question: q.question || '', type: q.type || 'TN', topic: q.topic || '', difficulty: q.difficulty || 'TH', answer: q.answer || '', solution_steps: q.solution_steps || [] })),
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