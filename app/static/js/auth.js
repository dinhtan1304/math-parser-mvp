/**
 * auth.js — Authentication, global state, tabs, user info, history, theme
 */

/* ===== Auth check ===== */
const token = localStorage.getItem('token');
if (!token) { window.location.href = '/login'; }
const authHeaders = { 'Authorization': 'Bearer ' + token };
window.authHeaders = authHeaders; // expose to other scripts (classes.js, etc.)

/* ===== Global state ===== */
let currentFile = null;
let currentResults = null;
let isProcessing = false;
let bankPage = 1;
const bankPageSize = 20;
let generatedResults = null;

/* ===== Theme ===== */
const savedTheme = localStorage.getItem('theme') || 'dark';
document.documentElement.setAttribute('data-theme', savedTheme);
updateThemeLabel(savedTheme);

window.toggleTheme = function() {
    const cur = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    updateThemeLabel(next);
};

function updateThemeLabel(theme) {
    const label = document.getElementById('themeLabel');
    if (label) label.textContent = theme === 'dark' ? 'Light Mode' : 'Dark Mode';
}

/* ===== Init on load ===== */
document.addEventListener('DOMContentLoaded', () => {
    loadUserInfo();
    loadHistory();
    loadDashboard();
    loadClasses();
});

/* ===== Tabs ===== */
window.switchTab = function(tab) {
    // Update sidebar nav items
    document.querySelectorAll('.nav-item[data-tab]').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });
    // Update topbar tabs
    document.querySelectorAll('.topbar-tab[data-tab]').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });
    // Switch panels
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    const panel = document.getElementById('tab' + tab.charAt(0).toUpperCase() + tab.slice(1));
    if (panel) panel.classList.add('active');

    if (tab === 'dashboard') loadDashboard();
    if (tab === 'bank')      loadBank();
    if (tab === 'generate')  { if (typeof loadGenFilters === 'function') loadGenFilters(); }
    if (tab === 'classes')   loadClasses();
};

/* ===== User info ===== */
async function loadUserInfo() {
    try {
        const res = await fetch('/api/v1/auth/me', { headers: authHeaders });
        if (!res.ok) throw new Error();
        const user = await res.json();
        const name = user.full_name || user.email;
        const el = document.getElementById('userName');
        if (el) el.textContent = name;
        // Avatar: first char
        const av = document.getElementById('userAvatar');
        if (av) av.textContent = name.charAt(0).toUpperCase();
        // Dashboard greeting
        const greet = document.getElementById('dashGreeting');
        if (greet) {
            const h = new Date().getHours();
            const prefix = h < 12 ? 'Chào buổi sáng' : h < 18 ? 'Chào buổi chiều' : 'Chào buổi tối';
            greet.textContent = `${prefix}, ${name}!`;
        }
    } catch { logout(); }
}

window.logout = function() {
    localStorage.removeItem('token');
    window.location.href = '/login';
};

window.showProfile = function() {
    toast('Tính năng hồ sơ đang phát triển', 'info');
};

/* ===== History sidebar (for upload tab) ===== */
async function loadHistory() {
    try {
        const res = await fetch('/api/v1/parser/history', { headers: authHeaders });
        if (!res.ok) return;
        const data = await res.json();
        renderHistory(data.items || []);
    } catch (e) { console.error('History error', e); }
}

function renderHistory(exams) {
    const list = document.getElementById('historyList');
    if (!list) return;
    if (!exams.length) {
        list.innerHTML = '<div class="empty-state" style="padding:24px"><div class="empty-sub">Chưa có đề nào</div></div>';
        return;
    }
    list.innerHTML = exams.map(e => `
        <div class="history-item ${e.status === 'completed' ? '' : ''}" onclick="window._loadExam(${e.id})">
            <div class="history-name">${esc(e.filename)}</div>
            <div class="history-meta">${fmtDate(e.created_at)} · ${e.status === 'completed' ? '<span class="text-green">✓</span>' : e.status}</div>
        </div>
    `).join('');
}

/* ===== Sidebar mobile ===== */
window.toggleSidebar = function() {
    document.getElementById('sidebar').classList.toggle('open');
    document.getElementById('sidebarOverlay').classList.toggle('open');
};