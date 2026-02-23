/**
 * dashboard.js — Dashboard stats, charts, activity
 */

let _charts = {};

function showDashSkeleton() {
    ['dTotal','dExams','dTopics','dNewWeek'].forEach(id => {
        $(id).innerHTML = '<span class="skeleton skeleton-stat"></span>';
    });
    $('dGrowth').textContent = '';
}

async function loadDashboard() {
    showDashSkeleton();
    try {
        const [statsRes, chartsRes, actRes] = await Promise.all([
            fetch('/api/v1/dashboard', { headers: authHeaders }),
            fetch('/api/v1/dashboard/charts', { headers: authHeaders }),
            fetch('/api/v1/dashboard/activity', { headers: authHeaders }),
        ]);

        if (statsRes.ok) {
            const s = await statsRes.json();
            $('dTotal').textContent = s.total_questions.toLocaleString();
            $('dExams').textContent = s.total_exams;
            $('dTopics').textContent = s.topics_count;
            $('dNewWeek').textContent = s.new_this_week;

            const g = s.growth_percent;
            const badge = $('dGrowth');
            if (g > 0) {
                badge.className = 'stat-badge up';
                badge.textContent = `↑ +${g}% so với tuần trước`;
            } else if (g < 0) {
                badge.className = 'stat-badge down';
                badge.textContent = `↓ ${g}% so với tuần trước`;
            } else {
                badge.className = 'stat-badge neutral';
                badge.textContent = 'Tuần này';
            }
        }

        if (chartsRes.ok) {
            const c = await chartsRes.json();
            renderDifficultyChart(c.by_difficulty);
            renderTopicChart(c.by_topic);
            renderTypeChart(c.by_type);
        }

        if (actRes.ok) {
            const a = await actRes.json();
            renderActivity(a.activities);
        }
    } catch (e) { console.error('Dashboard error', e); }
}

function renderDifficultyChart(data) {
    const labels = ['NB', 'TH', 'VD', 'VDC'];
    const colors = ['#60a5fa', '#34d399', '#fbbf24', '#f87171'];
    const values = labels.map(l => data[l] || 0);
    const total = values.reduce((a, b) => a + b, 0);
    if (_charts.diff) _charts.diff.destroy();
    if (!total) return;
    _charts.diff = new Chart($('chartDifficulty'), {
        type: 'doughnut',
        data: { labels: labels.map((l, i) => `${l} (${values[i]})`), datasets: [{ data: values, backgroundColor: colors, borderWidth: 0, hoverOffset: 6 }] },
        options: { responsive: true, maintainAspectRatio: false, cutout: '65%', plugins: { legend: { position: 'right', labels: { padding: 12, usePointStyle: true, pointStyleWidth: 10, font: { size: 12, family: "'Plus Jakarta Sans'" } } } } }
    });
}

function renderTopicChart(data) {
    const entries = Object.entries(data).slice(0, 8);
    if (!entries.length) return;
    if (_charts.topic) _charts.topic.destroy();
    const gradient = ['#6366f1','#8b5cf6','#a78bfa','#c4b5fd','#818cf8','#6366f1','#4f46e5','#4338ca'];
    _charts.topic = new Chart($('chartTopics'), {
        type: 'bar',
        data: { labels: entries.map(e => e[0].length > 14 ? e[0].slice(0, 12) + '…' : e[0]), datasets: [{ data: entries.map(e => e[1]), backgroundColor: entries.map((_, i) => gradient[i % gradient.length]), borderRadius: 6, barThickness: 22 }] },
        options: { responsive: true, maintainAspectRatio: false, indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { grid: { display: false }, ticks: { font: { size: 11, family: "'Plus Jakarta Sans'" } } }, y: { grid: { display: false }, ticks: { font: { size: 11, family: "'Plus Jakarta Sans'" } } } } }
    });
}

function renderTypeChart(data) {
    const entries = Object.entries(data);
    if (!entries.length) return;
    if (_charts.type) _charts.type.destroy();
    _charts.type = new Chart($('chartType'), {
        type: 'doughnut',
        data: { labels: entries.map(e => `${e[0]} (${e[1]})`), datasets: [{ data: entries.map(e => e[1]), backgroundColor: ['#6366f1', '#f59e0b', '#10b981', '#f43f5e', '#8b5cf6'], borderWidth: 0 }] },
        options: { responsive: true, maintainAspectRatio: false, cutout: '60%', plugins: { legend: { position: 'right', labels: { padding: 8, usePointStyle: true, pointStyleWidth: 8, font: { size: 11, family: "'Plus Jakarta Sans'" } } } } }
    });
}

function renderActivity(activities) {
    const el = $('activityList');
    if (!activities || !activities.length) {
        el.innerHTML = '<div class="empty-state"><svg width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="16" cy="16" r="14"/><path d="M16 10v6l3 3"/></svg><p>Chưa có hoạt động</p></div>';
        return;
    }
    el.innerHTML = activities.slice(0, 8).map(a => `
        <div class="activity-item" onclick="window._loadExam(${a.id})">
            <div class="activity-dot ${a.status}"></div>
            <div class="activity-info">
                <div class="activity-name">${esc(a.filename)}</div>
                <div class="activity-meta">${a.created_at ? fmtDate(a.created_at) : ''}</div>
            </div>
            ${a.question_count ? `<div class="activity-count">${a.question_count} câu</div>` : ''}
        </div>
    `).join('');
}

window._loadExam = async (id) => {
    try {
        const res = await fetch('/api/v1/parser/status/' + id, { headers: authHeaders });
        if (!res.ok) return;
        const exam = await res.json();
        if (exam.status === 'completed' && exam.result_json) {
            switchTab('upload');
            displayResults(JSON.parse(exam.result_json));
            $('progressSection').classList.remove('visible');
        }
        // Close sidebar on mobile
        const sb = document.querySelector('.sidebar');
        if (sb) sb.classList.remove('mobile-open');
    } catch (e) { console.error(e); }
};