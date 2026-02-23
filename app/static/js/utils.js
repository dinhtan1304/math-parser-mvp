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