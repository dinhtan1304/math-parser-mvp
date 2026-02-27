/**
 * parser.js — File upload, SSE streaming progress (Task 18), results display
 */

/* ===== File handling ===== */
const fileIcons = { pdf:'📕', docx:'📘', doc:'📘', png:'🖼️', jpg:'🖼️', jpeg:'🖼️', txt:'📝', md:'📝' };
const MAX_FILE_SIZE_MB = 50;
const MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024;

document.addEventListener('DOMContentLoaded', () => {
    const ua = $('uploadArea');
    if (!ua) return;

    ua.addEventListener('click', (e) => { if (!e.target.closest('.file-remove')) $('fileInput').click(); });
    ua.addEventListener('dragover', e => { e.preventDefault(); ua.classList.add('dragover'); });
    ua.addEventListener('dragleave', () => ua.classList.remove('dragover'));
    ua.addEventListener('drop', e => { e.preventDefault(); ua.classList.remove('dragover'); if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]); });
    $('fileInput').addEventListener('change', () => { if ($('fileInput').files.length) setFile($('fileInput').files[0]); });
    $('fileRemove').addEventListener('click', (e) => { e.stopPropagation(); clearFile(); });

    /* ===== Upload button ===== */
    $('uploadBtn').addEventListener('click', doUpload);

    /* ===== Parse toolbar ===== */
    $('jsonToggle').addEventListener('click', () => $('jsonPanel').classList.toggle('visible'));
    $('downloadBtn').addEventListener('click', () => {
        if (!currentResults) return;
        const blob = new Blob([JSON.stringify(currentResults, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url;
        a.download = 'math_questions_' + Date.now() + '.json';
        a.click(); URL.revokeObjectURL(url);
    });
});

function setFile(file) {
    if (file.size > MAX_FILE_SIZE) {
        toast(`File quá lớn (${(file.size / 1024 / 1024).toFixed(1)}MB). Tối đa ${MAX_FILE_SIZE_MB}MB.`, 'error');
        return;
    }
    if (file.size === 0) { toast('File trống, vui lòng chọn file khác.', 'error'); return; }

    currentFile = file;
    const ext = file.name.split('.').pop().toLowerCase();
    $('fileThumb').textContent = fileIcons[ext] || '📄';
    $('fileName').textContent = file.name;
    $('fileSize').textContent = fmtSize(file.size);
    $('dropzoneDefault').style.display = 'none';
    $('filePreview').classList.add('visible');
    $('uploadArea').classList.add('has-file');
    $('uploadBtn').disabled = false;
}

function clearFile() {
    currentFile = null; $('fileInput').value = '';
    $('dropzoneDefault').style.display = '';
    $('filePreview').classList.remove('visible');
    $('uploadArea').classList.remove('has-file');
    $('uploadBtn').disabled = true;
}

/* ===== Upload + SSE streaming (Sprint 3, Task 18) ===== */
async function doUpload() {
    if (!currentFile || isProcessing) return;
    isProcessing = true;
    $('uploadBtn').classList.add('loading');
    $('uploadBtn').disabled = true;
    $('progressSection').classList.add('visible');
    $('resultsSection').classList.remove('visible');
    updateProgress(5, 'Đang tải lên...');

    try {
        const form = new FormData();
        form.append('file', currentFile);
        const url = '/api/v1/parser/parse?speed=balanced&use_vision=' + $('visionMode').checked;

        const res = await fetch(url, { method: 'POST', body: form, headers: authHeaders });
        if (!res.ok) {
            if (res.status === 401) { logout(); return; }
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'Upload thất bại');
        }

        const { job_id } = await res.json();
        loadHistory();

        // Try SSE first, fallback to polling
        await streamProgress(job_id);

    } catch (e) {
        $('progressSection').classList.remove('visible');
        toast(e.message, 'error');
    } finally {
        $('uploadBtn').classList.remove('loading');
        $('uploadBtn').disabled = !currentFile;
        isProcessing = false;
    }
}

function updateProgress(pct, text) {
    $('progressFill').style.width = pct + '%';
    $('progressText').textContent = text;
}

/**
 * SSE streaming progress — replaces polling (Task 18).
 * Server sends events: progress, complete, error.
 * Falls back to polling if SSE fails to connect.
 */
async function streamProgress(jobId) {
    return new Promise((resolve, reject) => {
        let settled = false;
        const sseUrl = `/api/v1/parser/stream/${jobId}?token=${encodeURIComponent(token)}`;
        const es = new EventSource(sseUrl);
        let fallbackTimer = null;

        // Fallback to polling if SSE doesn't connect within 5s
        fallbackTimer = setTimeout(() => {
            if (es.readyState !== EventSource.OPEN) {
                console.warn('SSE timeout, falling back to polling');
                es.close();
                pollStatus(jobId).then(resolve).catch(reject);
            }
        }, 5000);

        es.addEventListener('progress', (e) => {
            clearTimeout(fallbackTimer);
            try {
                const d = JSON.parse(e.data);
                updateProgress(d.percent || 50, d.message || 'Đang xử lý...');
            } catch {}
        });

        es.addEventListener('complete', (e) => {
            clearTimeout(fallbackTimer);
            es.close();
            if (settled) return;
            settled = true;

            updateProgress(100, 'Hoàn tất!');
            setTimeout(() => $('progressSection').classList.remove('visible'), 600);

            try {
                const d = JSON.parse(e.data);
                if (d.result_json) displayResults(JSON.parse(d.result_json));
                else if (d.questions) displayResults(d.questions);
            } catch {}

            loadHistory();
            loadBankFilters();
            toast('Phân tích hoàn tất! Câu hỏi đã lưu vào ngân hàng.', 'success');
            resolve();
        });

        es.addEventListener('error_event', (e) => {
            clearTimeout(fallbackTimer);
            es.close();
            if (settled) return;
            settled = true;
            $('progressSection').classList.remove('visible');
            loadHistory();
            try {
                const d = JSON.parse(e.data);
                reject(new Error(d.message || 'Phân tích thất bại'));
            } catch {
                reject(new Error('Phân tích thất bại'));
            }
        });

        es.onerror = () => {
            // SSE connection error — fallback to polling
            clearTimeout(fallbackTimer);
            es.close();
            if (settled) return;
            settled = true;
            console.warn('SSE error, falling back to polling');
            pollStatus(jobId).then(resolve).catch(reject);
        };
    });
}

/* ===== Polling fallback ===== */
async function pollStatus(jobId) {
    let tries = 0;
    while (tries < 300) {
        tries++;
        const res = await fetch('/api/v1/parser/status/' + jobId, { headers: authHeaders });
        if (!res.ok) { if (res.status === 401) logout(); throw new Error('Kiểm tra trạng thái thất bại'); }
        const data = await res.json();

        if (data.status === 'processing') {
            updateProgress(50, 'AI đang phân tích câu hỏi...');
        } else if (data.status === 'completed') {
            updateProgress(100, 'Hoàn tất!');
            setTimeout(() => $('progressSection').classList.remove('visible'), 600);
            if (data.result_json) displayResults(JSON.parse(data.result_json));
            loadHistory();
            toast('Phân tích hoàn tất! Câu hỏi đã lưu vào ngân hàng.', 'success');
            loadBankFilters();
            return;
        } else if (data.status === 'failed') {
            $('progressSection').classList.remove('visible');
            loadHistory();
            throw new Error(data.error_message || 'Phân tích thất bại');
        }
        await new Promise(r => setTimeout(r, 2000));
    }
}

/* ===== Display parse results ===== */
function displayResults(questions) {
    currentResults = questions;
    $('resultsSection').classList.add('visible');
    $('resultsCount').innerHTML = '<span>' + questions.length + '</span> câu hỏi';
    $('jsonOutput').textContent = JSON.stringify(questions, null, 2);
    $('questionsContainer').innerHTML = questions.map((q, i) => renderQCard(q, i + 1)).join('');
    renderMath($('questionsContainer'));
}

/* ===== Shared Q-card renderer ===== */
function renderQCard(q, num, showSource) {
    const text = q.question_text || q.question || '';
    const qId = q.id || 0;

    let steps = q.solution_steps || [];
    if (typeof steps === 'string') { try { steps = JSON.parse(steps); } catch { steps = []; } }

    const actions = qId ? `<div class="q-actions">
        <button class="q-act-btn q-edit-btn" onclick="openEditModal(${qId})" title="Sửa">✏️</button>
        <button class="q-act-btn q-del-btn" onclick="deleteQuestion(${qId})" title="Xóa">🗑️</button>
    </div>` : '';

    const ans = q.answer || '';

    return `<div class="q-card" data-qid="${qId}">
        <div class="q-top">
            <span class="q-num">Câu ${num}</span>
            <div class="q-badges">
                ${renderCurriculumBadges(q)}
                ${actions}
            </div>
        </div>
        <div class="q-text">${esc(text)}</div>
        ${ans ? `<div class="q-answer"><div class="q-answer-label">Đáp án</div><div class="q-answer-text">${esc(ans)}</div></div>` : ''}
        ${steps.length ? `<div class="q-solution"><div class="q-solution-label">Lời giải</div><ul class="q-steps">${steps.map(s => '<li>' + esc(s) + '</li>').join('')}</ul></div>` : ''}
        ${showSource ? `<div class="q-source">Nguồn: <span class="q-source-name">${esc(showSource)}</span></div>` : ''}
    </div>`;
}