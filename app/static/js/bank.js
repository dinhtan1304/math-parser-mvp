/**
 * bank.js — Question bank: filters, list, pagination, edit modal, delete
 */

document.addEventListener('DOMContentLoaded', () => {
    /* ===== Filter event listeners ===== */
    // Debounced search
    let _searchTimer = null;
    function debouncedSearch(delay) {
        clearTimeout(_searchTimer);
        _searchTimer = setTimeout(() => { bankPage = 1; loadBankQuestions(); }, delay);
    }
    $('filterKeyword').addEventListener('input', () => debouncedSearch(400));
    $('filterKeyword').addEventListener('keydown', e => {
        if (e.key === 'Enter') { clearTimeout(_searchTimer); searchBank(); }
    });
    $('filterType').addEventListener('change', () => { bankPage = 1; loadBankQuestions(); });
    $('filterTopic').addEventListener('change', () => { bankPage = 1; loadBankQuestions(); });
    $('filterDiff').addEventListener('change', () => { bankPage = 1; loadBankQuestions(); });

    // Edit modal events
    $('editModal').addEventListener('click', (e) => {
        if (e.target === $('editModal')) closeEditModal();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && $('editModal').classList.contains('visible')) closeEditModal();
    });

    // Initial load
    loadBankFilters();
});

/* ===== Load bank ===== */
async function loadBank() {
    await loadBankFilters();
    await loadBankQuestions();
}

async function loadBankFilters() {
    try {
        const res = await fetch('/api/v1/questions/filters', { headers: authHeaders });
        if (!res.ok) return;
        const f = await res.json();
        $('bankCount').textContent = f.total_questions;
        $('statTotal').textContent = f.total_questions;
        if ($('statTypes')) $('statTypes').textContent = f.types.length;
        $('statTopics').textContent = f.topics.length;
        populateSelect($('filterType'), f.types, 'Tất cả dạng');
        populateSelect($('filterTopic'), f.topics, 'Tất cả chủ đề');
        populateSelect($('filterDiff'), f.difficulties, 'Tất cả độ khó');
    } catch (e) { console.error('Bank filters error', e); }
}

function populateSelect(el, values, placeholder) {
    const current = el.value;
    el.innerHTML = `<option value="">${placeholder}</option>` +
        values.map(v => `<option value="${esc(v)}" ${v === current ? 'selected' : ''}>${esc(v)}</option>`).join('');
}

function showBankSkeleton() {
    const skeletonHTML = Array.from({length: 3}, () => `
        <div class="skeleton-card">
            <div style="display:flex;justify-content:space-between;margin-bottom:12px;">
                <span class="skeleton skeleton-badge"></span>
                <div><span class="skeleton skeleton-badge"></span><span class="skeleton skeleton-badge"></span><span class="skeleton skeleton-badge"></span></div>
            </div>
            <div class="skeleton skeleton-text w80"></div>
            <div class="skeleton skeleton-text w60"></div>
        </div>
    `).join('');
    $('bankQuestionsList').innerHTML = skeletonHTML;
    $('bankPagination').innerHTML = '';
}

async function loadBankQuestions() {
    showBankSkeleton();
    const params = new URLSearchParams();
    params.set('page', bankPage);
    params.set('page_size', bankPageSize);

    const type = $('filterType').value;
    const topic = $('filterTopic').value;
    const diff = $('filterDiff').value;
    const keyword = $('filterKeyword').value.trim();

    if (type) params.set('type', type);
    if (topic) params.set('topic', topic);
    if (diff) params.set('difficulty', diff);
    if (keyword) params.set('keyword', keyword);

    try {
        const res = await fetch('/api/v1/questions?' + params.toString(), { headers: authHeaders });
        if (!res.ok) return;
        const data = await res.json();

        $('bankResultsCount').innerHTML = `Kết quả: <span>${data.total}</span> câu`;

        const list = $('bankQuestionsList');
        if (!data.items.length) {
            list.innerHTML = `
                <div class="bank-empty">
                    <div class="bank-empty-icon">📚</div>
                    <div class="bank-empty-text">${keyword || type || topic || diff ? 'Không tìm thấy câu hỏi phù hợp' : 'Ngân hàng câu hỏi trống'}</div>
                    <div class="bank-empty-hint">${keyword || type || topic || diff ? 'Thử thay đổi bộ lọc' : 'Upload đề thi để bắt đầu tích lũy câu hỏi'}</div>
                </div>`;
            $('bankPagination').innerHTML = '';
            return;
        }

        list.innerHTML = data.items.map((q, i) => {
            const num = (data.page - 1) * data.page_size + i + 1;
            return renderQCard(q, num, false);
        }).join('');

        renderMath(list);
        renderPagination(data.total, data.page, data.page_size);

    } catch (e) { console.error('Bank questions error', e); }
}

function renderPagination(total, page, pageSize) {
    const totalPages = Math.ceil(total / pageSize);
    if (totalPages <= 1) { $('bankPagination').innerHTML = ''; return; }

    let html = '';
    html += `<button class="page-btn" ${page <= 1 ? 'disabled' : ''} onclick="window._bankPage(${page - 1})">‹</button>`;
    const start = Math.max(1, page - 2);
    const end = Math.min(totalPages, page + 2);
    if (start > 1) html += `<button class="page-btn" onclick="window._bankPage(1)">1</button>`;
    if (start > 2) html += `<span class="page-info">...</span>`;
    for (let i = start; i <= end; i++) {
        html += `<button class="page-btn ${i === page ? 'active' : ''}" onclick="window._bankPage(${i})">${i}</button>`;
    }
    if (end < totalPages - 1) html += `<span class="page-info">...</span>`;
    if (end < totalPages) html += `<button class="page-btn" onclick="window._bankPage(${totalPages})">${totalPages}</button>`;
    html += `<button class="page-btn" ${page >= totalPages ? 'disabled' : ''} onclick="window._bankPage(${page + 1})">›</button>`;
    $('bankPagination').innerHTML = html;
}

window._bankPage = function(p) { bankPage = p; loadBankQuestions(); };
window.searchBank = function() { bankPage = 1; loadBankQuestions(); };
window.resetFilters = function() {
    $('filterType').value = ''; $('filterTopic').value = ''; $('filterDiff').value = ''; $('filterKeyword').value = '';
    bankPage = 1; loadBankQuestions();
};

/* ===== Edit modal ===== */
window.openEditModal = async function(qId) {
    try {
        const res = await fetch(`/api/v1/questions/${qId}`, { headers: authHeaders });
        if (!res.ok) throw new Error('Không tải được câu hỏi');
        const q = await res.json();
        $('editQId').value = q.id;
        $('editQText').value = q.question_text || '';
        $('editQType').value = q.question_type || '';
        $('editQTopic').value = q.topic || '';
        $('editQDiff').value = q.difficulty || 'TH';
        $('editQAnswer').value = q.answer || '';
        let steps = q.solution_steps || '[]';
        if (typeof steps === 'string') { try { steps = JSON.parse(steps); } catch { steps = []; } }
        $('editQSteps').value = JSON.stringify(steps, null, 2);
        $('editModal').classList.add('visible');
    } catch (e) { toast(e.message, 'error'); }
};

window.closeEditModal = function() { $('editModal').classList.remove('visible'); };

window.submitEdit = async function() {
    const qId = $('editQId').value;
    if (!qId) return;
    const btn = $('editSaveBtn');
    btn.disabled = true; btn.textContent = 'Đang lưu...';
    try {
        const body = {
            question_text: $('editQText').value, question_type: $('editQType').value,
            topic: $('editQTopic').value, difficulty: $('editQDiff').value,
            answer: $('editQAnswer').value, solution_steps: $('editQSteps').value,
        };
        const res = await fetch(`/api/v1/questions/${qId}`, {
            method: 'PUT', headers: { ...authHeaders, 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || 'Cập nhật thất bại'); }
        toast('✅ Đã cập nhật câu hỏi', 'success');
        closeEditModal(); loadBankQuestions();
    } catch (e) { toast(e.message, 'error'); }
    finally { btn.disabled = false; btn.textContent = 'Lưu thay đổi'; }
};

window.deleteQuestion = async function(qId) {
    if (!confirm('Xóa câu hỏi này?')) return;
    try {
        const res = await fetch(`/api/v1/questions/${qId}`, { method: 'DELETE', headers: authHeaders });
        if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || 'Xóa thất bại'); }
        toast('Đã xóa câu hỏi', 'success'); loadBankQuestions(); loadBankFilters();
    } catch (e) { toast(e.message, 'error'); }
};