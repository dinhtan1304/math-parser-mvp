/**
 * classes.js — Classroom management v2
 * Features: list/create/edit/delete class, member search/remove,
 * assignment management + submission view, analytics tab, leaderboard.
 */

let currentClassId = null;
let currentClass   = null;
let allClasses     = [];
let allMembers     = [];

const AV_COLORS = ['av-0','av-1','av-2','av-3','av-4','av-5','av-6','av-7'];
const STRIPE_COLORS = ['stripe-0','stripe-1','stripe-2','stripe-3','stripe-4','stripe-5'];

function avColor(seed) {
    let h = 0;
    for (let i = 0; i < (seed||'').length; i++) h = (h*31 + seed.charCodeAt(i)) & 0x7fffffff;
    return AV_COLORS[h % AV_COLORS.length];
}
function stripeColor(idx) { return STRIPE_COLORS[idx % STRIPE_COLORS.length]; }
function initials(name) { return (name||'?').split(' ').map(w=>w[0]).join('').toUpperCase().slice(0,2); }

async function apiFetch(url, opts = {}) {
    const _token = localStorage.getItem('token');
    if (!_token) { window.location.href = '/login'; return; }
    const res = await fetch(url, {
        ...opts,
        headers: {
            'Authorization': 'Bearer ' + _token,
            'Content-Type': 'application/json',
            ...(opts.headers || {}),
        },
    });
    if (res.status === 401) { localStorage.removeItem('token'); window.location.href = '/login'; return; }
    if (res.status === 204) return null;
    const text = await res.text();
    let data; try { data = JSON.parse(text); } catch { data = text; }
    if (!res.ok) throw new Error(typeof data==='string'?data:(data?.detail||res.statusText));
    return data;
}

/* ── LOAD ALL CLASSES ── */
window.loadClasses = async function () {
    const grid = $('classesGrid');
    grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1"><div class="spinner"></div></div>';
    try {
        allClasses = await apiFetch('/api/v1/classes');
        const totalStudents = allClasses.reduce((s,c)=>s+(c.member_count||0), 0);
        const totalAssignments = allClasses.reduce((s,c)=>s+(c.assignment_count||0), 0);
        if (allClasses.length) {
            $('summaryClasses').textContent = allClasses.length;
            $('summaryStudents').textContent = totalStudents;
            $('summaryAssignments').textContent = totalAssignments;
            $('classSummaryBar').style.display = 'flex';
        } else {
            $('classSummaryBar').style.display = 'none';
        }
        const badge = $('classBadge');
        if (badge) { badge.textContent = allClasses.length; badge.classList.toggle('hidden', !allClasses.length); }
        const s1 = $('statClasses'); if (s1) s1.textContent = allClasses.length;
        const s2 = $('statStudents'); if (s2) s2.textContent = totalStudents;

        if (!allClasses.length) {
            grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1">
              <div class="empty-icon">🏫</div>
              <div class="empty-title">Chưa có lớp nào</div>
              <div class="empty-sub">Tạo lớp học đầu tiên để bắt đầu giao bài</div>
              <button class="btn btn-primary" style="margin-top:16px" onclick="openCreateClassModal()">+ Tạo lớp ngay</button>
            </div>`;
            return;
        }
        grid.innerHTML = allClasses.map((cls, i) => classCardHtml(cls, i)).join('');
    } catch (e) {
        grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1">
          <div class="empty-icon">⚠️</div><div class="empty-title text-red">Lỗi tải dữ liệu</div>
          <div class="empty-sub">${esc(e.message)}</div>
          <button class="btn btn-secondary" style="margin-top:12px" onclick="loadClasses()">Thử lại</button>
        </div>`;
    }
};

function classCardHtml(cls, idx) {
    const meta = [cls.subject, cls.grade?`Lớp ${cls.grade}`:''].filter(Boolean).join(' · ') || 'Chưa phân loại';
    return `
    <div class="class-card" onclick="openClassDetail(${cls.id})">
      <div class="class-card-stripe ${stripeColor(idx)}"></div>
      <div class="class-card-code">${esc(cls.code)}</div>
      <div class="class-card-name">${esc(cls.name)}</div>
      <div class="class-card-meta">
        <span class="class-card-active-dot" style="background:${cls.is_active?'var(--green)':'var(--text-3)'}"></span>
        ${esc(meta)} · ${cls.is_active?'Đang mở':'Đã đóng'}
      </div>
      <div class="class-card-stats">
        <div class="class-stat"><div class="class-stat-val">${cls.member_count||0}</div><div class="class-stat-lbl">Học sinh</div></div>
        <div class="class-stat"><div class="class-stat-val">${cls.assignment_count||0}</div><div class="class-stat-lbl">Bài tập</div></div>
        <div class="class-stat"><div class="class-stat-val text-accent">→</div><div class="class-stat-lbl">Chi tiết</div></div>
      </div>
      <div class="class-card-actions" onclick="event.stopPropagation()">
        <button class="btn btn-sm btn-ghost" style="flex:1" onclick="copyCode('${esc(cls.code)}')">
          <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
          Copy mã
        </button>
        <button class="btn btn-sm btn-primary" style="flex:1" onclick="openSendToClass(${cls.id})">
          <svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>
          Giao bài
        </button>
      </div>
    </div>`;
}

/* ── CLASS DETAIL ── */
window.openClassDetail = function (classId) {
    currentClassId = classId;
    currentClass = allClasses.find(c => c.id === classId) || { id: classId };
    $('detailCode').textContent = currentClass.code || '...';
    $('heroCodeLabel').textContent = currentClass.code || '...';
    $('detailName').textContent = currentClass.name || '...';
    $('detailMeta').textContent = [currentClass.subject, currentClass.grade?`Lớp ${currentClass.grade}`:''].filter(Boolean).join(' · ') || 'Chưa phân loại';
    $('heroStudents').textContent = currentClass.member_count ?? '...';
    $('heroAssignments').textContent = currentClass.assignment_count ?? '...';
    $('heroCompletion').textContent = '...';
    $('classesListView').classList.add('hidden');
    $('classDetailView').classList.remove('hidden');
    showDetailTab('members');
};

window.showClassesList = function () {
    $('classDetailView').classList.add('hidden');
    $('classesListView').classList.remove('hidden');
    currentClassId = null; currentClass = null;
};

window.showDetailTab = function (tab) {
    ['members','assignments','analytics','leaderboard'].forEach(t => {
        const btn = document.querySelector(`[data-detail="${t}"]`);
        const id = 'detail' + t.charAt(0).toUpperCase() + t.slice(1);
        const panel = $(id);
        if (btn) btn.classList.toggle('active', t === tab);
        if (panel) panel.classList.toggle('hidden', t !== tab);
    });
    if (tab==='members')     loadMembers();
    if (tab==='assignments') loadAssignments();
    if (tab==='analytics')   loadAnalytics();
    if (tab==='leaderboard') loadLeaderboard();
};

/* ── MEMBERS ── */
async function loadMembers() {
    const list = $('membersList');
    list.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
    try {
        const members = await apiFetch(`/api/v1/classes/${currentClassId}/members`);
        allMembers = members;
        $('tabBadgeMembers').textContent = members.length;
        $('memberCount').textContent = `${members.length} học sinh`;
        $('heroStudents').textContent = members.length;
        renderMembers(members);
    } catch (e) {
        list.innerHTML = `<div class="empty-state text-red">${esc(e.message)}</div>`;
    }
}

function renderMembers(members) {
    const list = $('membersList');
    if (!members.length) {
        list.innerHTML = `<div class="empty-state">
          <div class="empty-icon">👥</div>
          <div class="empty-title">Chưa có học sinh nào</div>
          <div class="empty-sub">Chia sẻ mã <strong>${esc(currentClass?.code||'')}</strong> để học sinh tham gia</div>
          <button class="btn btn-secondary" style="margin-top:12px" onclick="copyClassCode()">📋 Copy mã lớp</button>
        </div>`; return;
    }
    list.innerHTML = members.map(m => {
        const av = avColor(m.student_email||m.student_name);
        return `<div class="member-row" id="member-${m.student_id}">
          <div class="member-ava ${av}">${initials(m.student_name)}</div>
          <div style="flex:1">
            <div class="member-name">${esc(m.student_name||'Chưa đặt tên')}</div>
            <div class="member-email">${esc(m.student_email||'')}</div>
          </div>
          <div class="member-joined text-muted">Tham gia ${fmtDate(m.joined_at)}</div>
          <button class="btn btn-sm btn-danger" onclick="removeMember(${m.student_id},event)">✕</button>
        </div>`;
    }).join('');
}

window.filterMembers = function (q) {
    q = q.toLowerCase().trim();
    renderMembers(q ? allMembers.filter(m=>(m.student_name||'').toLowerCase().includes(q)||(m.student_email||'').toLowerCase().includes(q)) : allMembers);
};

window.removeMember = async function (studentId, evt) {
    if (evt) evt.stopPropagation();
    const row = $(`member-${studentId}`);
    const name = row?.querySelector('.member-name')?.textContent || 'học sinh';
    if (!confirm(`Xóa "${name}" khỏi lớp?`)) return;
    try {
        await apiFetch(`/api/v1/classes/${currentClassId}/members/${studentId}`, {method:'DELETE'});
        allMembers = allMembers.filter(m=>m.student_id!==studentId);
        renderMembers(allMembers);
        $('tabBadgeMembers').textContent = allMembers.length;
        $('memberCount').textContent = `${allMembers.length} học sinh`;
        $('heroStudents').textContent = allMembers.length;
        toast(`Đã xóa ${name} khỏi lớp`, 'success');
    } catch (e) { toast(`Lỗi: ${e.message}`, 'error'); }
};

/* ── ASSIGNMENTS ── */
async function loadAssignments() {
    const list = $('assignmentsList');
    list.innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
    try {
        const items = await apiFetch(`/api/v1/assignments?class_id=${currentClassId}`);
        $('tabBadgeAssignments').textContent = items.length;
        $('heroAssignments').textContent = items.length;
        if (!items.length) {
            list.innerHTML = `<div class="empty-state">
              <div class="empty-icon">📋</div><div class="empty-title">Chưa có bài tập nào</div>
              <button class="btn btn-primary" style="margin-top:12px" onclick="openSendAssignmentModal()">📤 Giao bài ngay</button>
            </div>`; return;
        }
        if (items[0]) {
            const done = items[0].completed_count||0;
            const total = Math.max(allMembers.length||1, items[0].submission_count||1);
            $('heroCompletion').textContent = `${Math.round(done/total*100)}%`;
        }
        list.innerHTML = items.map(a => assignmentRowHtml(a)).join('');
    } catch (e) {
        list.innerHTML = `<div class="empty-state text-red">${esc(e.message)}</div>`;
    }
}

function assignmentRowHtml(a) {
    const deadline = a.deadline ? fmtDate(a.deadline) : 'Không giới hạn';
    const total = a.submission_count||0;
    const done = a.completed_count||0;
    const pct = total>0 ? Math.round(done/total*100) : 0;
    const expired = a.deadline && new Date(a.deadline)<new Date();
    const statusBadge = !a.is_active ? '<span class="badge badge-neutral">Đóng</span>'
        : expired ? '<span class="badge badge-red">Hết hạn</span>'
        : '<span class="badge badge-green">Đang mở</span>';
    return `<div class="assignment-row">
      <div class="assignment-icon" style="background:var(--accent-soft)">📋</div>
      <div class="assignment-info">
        <div class="assignment-title">${esc(a.title)}</div>
        <div class="assignment-meta">🕐 ${deadline} · 🔄 ${a.max_attempts} lần thử · ${fmtDate(a.created_at)}</div>
      </div>
      <div class="asgn-progress-wrap">
        <div class="asgn-progress-label"><span>Hoàn thành</span><span>${done}/${total}</span></div>
        <div class="asgn-progress-bg"><div class="asgn-progress-fill" style="width:${pct}%"></div></div>
      </div>
      <div class="asgn-stats">
        <div class="asgn-stat"><div class="asgn-stat-val">${total}</div><div class="asgn-stat-lbl">Đã nộp</div></div>
        <div class="asgn-stat"><div class="asgn-stat-val text-green">${done}</div><div class="asgn-stat-lbl">Xong</div></div>
      </div>
      <div class="asgn-actions">
        ${statusBadge}
        <button class="btn-icon" onclick="viewSubmissions(${a.id},'${esc(a.title)}')" title="Xem kết quả">
          <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
        </button>
        <button class="btn-icon" onclick="toggleAssignment(${a.id},${!a.is_active})" title="${a.is_active?'Đóng':'Mở lại'}">
          <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">${a.is_active?'<path d="M18.36 6.64a9 9 0 11-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>':'<circle cx="12" cy="12" r="10"/><polyline points="10 15 15 12 10 9"/>'}</svg>
        </button>
      </div>
    </div>`;
}

window.viewSubmissions = async function (assignmentId, title) {
    $('submissionsTitle').textContent = `Kết quả: ${title||'Bài tập'}`;
    $('submissionsMeta').textContent = '';
    $('submissionsList').innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
    $('scoreDistWrap').style.display = 'none';
    openModal('modalSubmissions');
    try {
        const subs = await apiFetch(`/api/v1/submissions/assignment/${assignmentId}`);
        $('submissionsMeta').textContent = `${subs.length} lần nộp bài`;
        if (!subs.length) {
            $('submissionsList').innerHTML = '<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-title">Chưa có ai nộp bài</div></div>';
            return;
        }
        // Score dist
        const dist = {'0-20':0,'21-40':0,'41-60':0,'61-80':0,'81-100':0};
        subs.forEach(s=>{const sc=s.score||0;if(sc<=20)dist['0-20']++;else if(sc<=40)dist['21-40']++;else if(sc<=60)dist['41-60']++;else if(sc<=80)dist['61-80']++;else dist['81-100']++;});
        const maxVal = Math.max(...Object.values(dist),1);
        const colors = ['#ef4444','#f97316','#f59e0b','#3b82f6','#10b981'];
        $('scoreBars').innerHTML = Object.entries(dist).map(([lbl,val],i)=>{
            const h = Math.max(4, Math.round(val/maxVal*44));
            return `<div class="cls-score-bar-item"><div style="height:${h}px;width:100%;background:${colors[i]};border-radius:3px 3px 0 0"></div><div class="cls-score-bar-label">${lbl}</div></div>`;
        }).join('');
        $('scoreDistWrap').style.display = 'block';

        const modeLabel={'multiple_choice':'⚡ Nhanh tay','drag_drop':'🧩 Ghép','fill_blank':'🔢 Điền','order_steps':'📊 Sắp xếp','find_error':'🔍 Lỗi','flashcard':'🃏 Thẻ'};
        subs.sort((a,b)=>(b.score||0)-(a.score||0));
        $('submissionsList').innerHTML = subs.map(s=>{
            const av = avColor(s.student_name);
            const scoreColor = s.score>=80?'var(--green)':s.score>=50?'var(--yellow)':'var(--red)';
            const timeStr = s.time_spent_s?`${Math.floor(s.time_spent_s/60)}p${s.time_spent_s%60}s`:'—';
            return `<div class="sub-row">
              <div class="sub-ava ${av}">${initials(s.student_name)}</div>
              <div class="sub-name">${esc(s.student_name||'Học sinh')}</div>
              <div class="sub-mode">${modeLabel[s.game_mode]||s.game_mode||'—'}</div>
              <div class="sub-time">⏱ ${timeStr}</div>
              <div style="font-size:11px;color:var(--text-3)">${s.correct_q||0}/${s.total_q||0} đúng</div>
              <div class="sub-score" style="color:${scoreColor}">${s.score??'—'}%</div>
            </div>`;
        }).join('');
    } catch (e) {
        $('submissionsList').innerHTML = `<div class="empty-state text-red">${esc(e.message)}</div>`;
    }
};

window.toggleAssignment = async function (id, active) {
    try {
        await apiFetch(`/api/v1/assignments/${id}`, {method:'PATCH', body:JSON.stringify({is_active:active})});
        toast(active?'Đã mở lại bài tập':'Đã đóng bài tập', 'success');
        loadAssignments();
    } catch (e) { toast(`Lỗi: ${e.message}`,'error'); }
};

/* ── ANALYTICS ── */
async function loadAnalytics() {
    ['anAvgScore','anCompletion','anActive7d'].forEach(id=>{$(id).textContent='...';});
    $('anTopStudents').innerHTML = $('anWeakTopics').innerHTML = $('anAllStudents').innerHTML =
        '<div class="empty-state"><div class="spinner"></div></div>';
    try {
        const d = await apiFetch(`/api/v1/analytics/class/${currentClassId}`);
        $('anAvgScore').textContent = d.avg_class_score!=null ? `${d.avg_class_score}%` : '—';
        $('anAvgScoreSub').textContent = `Trung bình ${d.total_students} học sinh`;
        $('anCompletion').textContent = d.completion_rate!=null ? `${d.completion_rate}%` : '—';
        $('anActive7d').textContent = d.active_last_7d ?? '—';
        $('anActive7dSub').textContent = `/ ${d.total_students} học sinh`;

        // Top students
        $('anTopStudents').innerHTML = (d.students||[]).slice(0,5).map((s,i)=>{
            const av = avColor(s.student_name);
            const medals = ['🥇','🥈','🥉','4️⃣','5️⃣'];
            return `<div class="cls-an-student-row">
              <div class="cls-an-rank">${medals[i]||i+1}</div>
              <div class="member-ava ${av}" style="width:28px;height:28px;font-size:11px">${initials(s.student_name)}</div>
              <div class="cls-an-name">${esc(s.student_name)}</div>
              <div class="cls-an-score">${s.avg_score??'—'}%</div>
            </div>`;
        }).join('') || '<div class="empty-state"><div class="empty-sub">Chưa có dữ liệu</div></div>';

        // Weak topics
        $('anWeakTopics').innerHTML = (d.topic_breakdown||[]).slice(0,5).map(t=>{
            const color = t.avg_score<40?'#ef4444':t.avg_score<60?'#f59e0b':'#10b981';
            return `<div class="cls-an-topic-row">
              <div class="cls-an-topic-name">${esc(t.topic)}</div>
              <div class="cls-an-topic-bar-wrap"><div class="cls-an-topic-bar">
                <div class="cls-an-topic-fill" style="width:${t.avg_score}%;background:${color}"></div>
              </div></div>
              <div style="font-size:11px;font-weight:700;color:${color};width:32px;text-align:right">${t.avg_score}%</div>
            </div>`;
        }).join('') || '<div class="empty-state"><div class="empty-sub">Chưa có dữ liệu chủ đề</div></div>';

        // Full table
        $('anAllStudents').innerHTML = (d.students||[]).length ? `
          <table class="cls-an-table">
            <thead><tr><th>Học sinh</th><th>Bài nộp</th><th>Điểm TB</th><th>XP</th><th>Streak</th><th>Lần cuối</th></tr></thead>
            <tbody>${(d.students||[]).map(s=>{
                const av = avColor(s.student_name);
                const sc = s.avg_score;
                const color = sc>=80?'#10b981':sc>=50?'#f59e0b':sc!=null?'#ef4444':'var(--text-3)';
                return `<tr>
                  <td><div style="display:flex;align-items:center;gap:8px">
                    <div class="member-ava ${av}" style="width:26px;height:26px;font-size:10px;flex-shrink:0">${initials(s.student_name)}</div>
                    <span style="font-weight:700;font-size:13px">${esc(s.student_name)}</span>
                  </div></td>
                  <td>${s.total_submissions}</td>
                  <td><div style="font-weight:800;font-size:13px;color:${color}">${sc??'—'}%</div>
                    <div class="cls-an-score-bar"><div class="cls-an-score-fill" style="width:${sc||0}%;background:${color}"></div></div>
                  </td>
                  <td style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:var(--accent)">${s.total_xp}</td>
                  <td>${s.streak_days>0?`🔥 ${s.streak_days}`:'—'}</td>
                  <td style="color:var(--text-3);font-size:11px">${s.last_active?fmtDate(s.last_active):'Chưa có'}</td>
                </tr>`;
            }).join('')}</tbody>
          </table>` : '<div class="empty-state"><div class="empty-sub">Chưa có dữ liệu</div></div>';

    } catch (e) {
        ['anAvgScore','anCompletion','anActive7d'].forEach(id=>{$(id).textContent='—';});
        $('anTopStudents').innerHTML = `<div class="empty-state text-red">${esc(e.message)}</div>`;
        $('anWeakTopics').innerHTML = $('anAllStudents').innerHTML = '';
    }
}

/* ── LEADERBOARD ── */
window.loadLeaderboard = async function () {
    $('lbList').innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
    try {
        const data = await apiFetch(`/api/v1/submissions/leaderboard/${currentClassId}`);
        [[2,'pod2'],[1,'pod1'],[3,'pod3']].forEach(([pos, id])=>{
            const e = data[pos-1];
            const avEl = $(`${id}Av`), nameEl = $(`${id}Name`), xpEl = $(`${id}XP`);
            if (e) {
                avEl.className = `cls-pod-avatar${pos===1?' cls-pod-avatar-1':''} ${avColor(e.student_name)}`;
                avEl.textContent = initials(e.student_name);
                nameEl.textContent = (e.student_name||'').split(' ').slice(-1)[0];
                xpEl.textContent = `${e.total_xp} XP`;
            } else {
                avEl.textContent = '?'; nameEl.textContent = '—'; xpEl.textContent = '0 XP';
            }
        });

        if (!data.length) {
            $('lbList').innerHTML = '<div class="empty-state"><div class="empty-icon">🏆</div><div class="empty-title">Chưa có xếp hạng</div><div class="empty-sub">Học sinh cần nộp bài để nhận XP</div></div>';
            return;
        }
        const levels = ['','⚡ Mới','🌟 Học viên','📚 Nỗ lực','🔥 Chăm chỉ','💡 Thông minh','🎯 Xuất sắc','🏆 Toán thủ','👑 Thiên tài'];
        $('lbList').innerHTML = data.map((e,i)=>{
            const av = avColor(e.student_name);
            const lvlLabel = levels[Math.min(e.level||1,8)] || `Lv.${e.level}`;
            const rankStr = i<3 ? ['🥇','🥈','🥉'][i] : `${i+1}`;
            return `<div class="cls-lb-row${e.is_me?' me':''}">
              <div class="cls-lb-rank">${rankStr}</div>
              <div class="cls-lb-ava ${av}">${initials(e.student_name)}</div>
              <div><div class="cls-lb-name">${esc(e.student_name)}${e.is_me?' <span style="font-size:10px;color:var(--accent)">(Bạn)</span>':''}</div>
                <div style="font-size:10px;color:var(--text-3)">${lvlLabel}</div></div>
              ${e.streak_days>0?`<div class="cls-lb-streak">🔥 ${e.streak_days}d</div>`:'<div></div>'}
              <div class="cls-lb-level">Lv.${e.level||1}</div>
              <div class="cls-lb-xp">${e.total_xp} XP</div>
            </div>`;
        }).join('');
    } catch (e) {
        $('lbList').innerHTML = `<div class="empty-state text-red">${esc(e.message)}</div>`;
    }
};

/* ── CREATE / EDIT CLASS ── */
window.openCreateClassModal = function () {
    ['newClassName','newClassSubject','newClassDesc'].forEach(id=>{$(id).value='';});
    $('newClassGrade').value = '';
    openModal('modalCreateClass');
    setTimeout(()=>$('newClassName').focus(),100);
};

window.createClass = async function () {
    const name = $('newClassName').value.trim();
    if (!name) { toast('Vui lòng nhập tên lớp','error'); return; }
    try {
        const cls = await apiFetch('/api/v1/classes', {method:'POST', body:JSON.stringify({
            name, subject:$('newClassSubject').value.trim()||null,
            grade:$('newClassGrade').value?+$('newClassGrade').value:null,
            description:$('newClassDesc').value.trim()||null,
        })});
        closeModal('modalCreateClass');
        toast(`🏫 Tạo lớp "${cls.name}" — Mã: ${cls.code}`,'success');
        loadClasses();
    } catch (e) { toast(`Lỗi: ${e.message}`,'error'); }
};

window.openEditClassModal = function () {
    if (!currentClass) return;
    $('editClassName').value = currentClass.name||'';
    $('editClassSubject').value = currentClass.subject||'';
    $('editClassGrade').value = currentClass.grade||'';
    $('editClassDesc').value = currentClass.description||'';
    $('editClassActive').checked = currentClass.is_active!==false;
    openModal('modalEditClass');
};

window.saveEditClass = async function () {
    const name = $('editClassName').value.trim();
    if (!name) { toast('Tên lớp không được để trống','error'); return; }
    try {
        const updated = await apiFetch(`/api/v1/classes/${currentClassId}`, {method:'PATCH', body:JSON.stringify({
            name, subject:$('editClassSubject').value.trim()||null,
            grade:$('editClassGrade').value?+$('editClassGrade').value:null,
            description:$('editClassDesc').value.trim()||null,
            is_active:$('editClassActive').checked,
        })});
        closeModal('modalEditClass');
        currentClass = {...currentClass, ...updated};
        allClasses = allClasses.map(c=>c.id===currentClassId?currentClass:c);
        $('detailName').textContent = updated.name;
        $('detailMeta').textContent = [updated.subject, updated.grade?`Lớp ${updated.grade}`:''].filter(Boolean).join(' · ')||'Chưa phân loại';
        toast('Đã cập nhật thông tin lớp','success');
    } catch (e) { toast(`Lỗi: ${e.message}`,'error'); }
};

window.deleteCurrentClass = async function () {
    if (!confirm(`Xóa lớp "${currentClass?.name}"? Dữ liệu sẽ bị xóa vĩnh viễn.`)) return;
    try {
        await apiFetch(`/api/v1/classes/${currentClassId}`,{method:'DELETE'});
        closeModal('modalEditClass');
        toast('Đã xóa lớp học','success');
        showClassesList(); loadClasses();
    } catch (e) { toast(`Lỗi: ${e.message}`,'error'); }
};

/* ── SEND ASSIGNMENT ── */
window.openSendAssignmentModal = async function () {
    $('assignTitle').value=$('assignDesc').value=$('assignDeadline').value='';
    $('assignAttempts').value='3'; $('assignShowAnswer').checked=true;
    try {
        const exams = await apiFetch('/api/v1/parser/list');
        $('assignExam').innerHTML = '<option value="">— Chọn đề đã phân tích —</option>'+
            exams.filter(e=>e.status==='completed').map(e=>`<option value="${e.id}">${esc(e.filename)} (${fmtDate(e.created_at)})</option>`).join('');
    } catch { $('assignExam').innerHTML='<option value="">Không tải được danh sách đề</option>'; }

    const listEl = $('assignClassList');
    listEl.innerHTML = allClasses.length
        ? allClasses.map(cls=>`<label class="send-class-check">
            <input type="checkbox" name="assignClass" value="${cls.id}" ${currentClassId==cls.id?'checked':''}>
            <div><div style="font-size:13px;font-weight:700">${esc(cls.name)}</div>
            <div class="text-muted" style="font-size:11px">${cls.member_count||0} học sinh</div></div>
            <span class="send-class-code">${esc(cls.code)}</span>
          </label>`).join('')
        : '<div class="text-muted" style="font-size:13px;padding:10px 0">Chưa có lớp nào.</div>';

    openModal('modalSendAssignment');
    setTimeout(()=>$('assignTitle').focus(),100);
};

window.openSendToClass = function (classId) {
    currentClassId = classId;
    currentClass = allClasses.find(c=>c.id===classId);
    openSendAssignmentModal();
};

window.sendAssignment = async function () {
    const title = $('assignTitle').value.trim();
    if (!title) { toast('Vui lòng nhập tiêu đề','error'); return; }
    const selected = [...document.querySelectorAll('input[name="assignClass"]:checked')].map(el=>+el.value);
    if (!selected.length) { toast('Chọn ít nhất 1 lớp','error'); return; }
    const examId = $('assignExam').value?+$('assignExam').value:null;
    const deadline = $('assignDeadline').value;
    try {
        await apiFetch('/api/v1/assignments/send-to-classes', {method:'POST', body:JSON.stringify({
            exam_id:examId||0, class_ids:selected, title,
            description:$('assignDesc').value.trim()||null,
            deadline:deadline?new Date(deadline).toISOString():null,
            max_attempts:+$('assignAttempts').value||3,
            show_answer:$('assignShowAnswer').checked,
        })});
        closeModal('modalSendAssignment');
        toast(`✅ Đã giao bài cho ${selected.length} lớp`,'success');
        if (currentClassId) loadAssignments();
        loadClasses();
    } catch (e) { toast(`Lỗi: ${e.message}`,'error'); }
};

/* ── MODAL / UTILS ── */
window.copyClassCode = function() { const code=currentClass?.code; if(code) copyCode(code); };
window.copyCode = function(code) { navigator.clipboard.writeText(code).then(()=>toast(`📋 Copy mã "${code}"`,'success')); };

window.openModal = function(id) { $(id).classList.add('open'); document.body.style.overflow='hidden'; };
window.closeModal = function(id) { $(id).classList.remove('open'); document.body.style.overflow=''; };

document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', e => { if(e.target===overlay) closeModal(overlay.id); });
});
document.addEventListener('keydown', e => {
    if (e.key==='Escape') document.querySelectorAll('.modal-overlay.open').forEach(m=>closeModal(m.id));
});