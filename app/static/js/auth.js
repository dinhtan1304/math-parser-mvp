/**
 * auth.js — Authentication, global state, tabs, user info, history
 */

/* ===== Auth check ===== */
const token = localStorage.getItem('token');
if (!token) { window.location.href = '/login'; }
const authHeaders = { 'Authorization': 'Bearer ' + token };

/* ===== Global state ===== */
let currentFile = null;
let currentResults = null;
let isProcessing = false;
let bankPage = 1;
const bankPageSize = 20;
let generatedResults = null;

/* ===== Init on load ===== */
document.addEventListener('DOMContentLoaded', () => {
    loadUserInfo();
    loadHistory();
    loadDashboard();
});

/* ===== Tabs ===== */
window.switchTab = function(tab) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    $('tab' + tab.charAt(0).toUpperCase() + tab.slice(1)).classList.add('active');

    // Widen main-body for generator split layout
    document.querySelector('.main-body').classList.toggle('wide', tab === 'generate');

    if (tab === 'dashboard') loadDashboard();
    if (tab === 'bank') loadBank();
    if (tab === 'generate') loadGenFilters();
};

/* ===== User info ===== */
async function loadUserInfo() {
    try {
        const res = await fetch('/api/v1/auth/me', { headers: authHeaders });
        if (!res.ok) throw new Error();
        const user = await res.json();
        $('userName').textContent = user.full_name || user.email;
    } catch { logout(); }
}

window.logout = function() {
    localStorage.removeItem('token');
    window.location.href = '/login';
};

/* ===== History sidebar ===== */
async function loadHistory() {
    try {
        const res = await fetch('/api/v1/parser/history', { headers: authHeaders });
        if (!res.ok) return;
        const data = await res.json();
        renderHistory(data.items || []);
    } catch (e) { console.error('History error', e); }
}

function renderHistory(exams) {
    const list = $('historyList');
    if (!list) return;
    list.innerHTML = exams.map(e => `
        <div class="history-item" onclick="window._loadExam(${e.id})" title="${esc(e.filename)}">
            <div class="history-item-name">${esc(e.filename)}</div>
            <div class="history-item-meta">
                <div class="status-dot ${e.status}"></div>
                <span class="history-item-date">${fmtDate(e.created_at)}</span>
            </div>
        </div>
    `).join('');
}

/* ===== Sidebar mobile ===== */
window.toggleSidebar = function() {
    document.querySelector('.sidebar').classList.toggle('mobile-open');
};
document.addEventListener('click', (e) => {
    const sidebar = document.querySelector('.sidebar');
    if (sidebar && sidebar.classList.contains('mobile-open') && !e.target.closest('.sidebar') && !e.target.closest('.mobile-menu-btn')) {
        sidebar.classList.remove('mobile-open');
    }
});