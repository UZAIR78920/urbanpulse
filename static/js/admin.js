// UrbanPulse — admin console
(function () {
  const $ = (id) => document.getElementById(id);
  const filters = { status: '', category: '', q: '' };
  let verifiedPoliticians;
  const STATUS_COLORS = {
    Reported: '#E0912B', 'Under Review': '#D18B25', Acknowledged: '#2C8C8C',
    Assigned: '#287A7A', 'In Progress': '#2E56C7', Resolved: '#3C7A58', Verified: '#316849',
  };
  const money = (n) => '₹' + Math.round(n).toLocaleString('en-IN');
  const empty = (text) => `<p style="font-size:13px;color:var(--text-muted);">${text}</p>`;

  // ------------------------------------------------------------- overview
  async function loadStats() {
    const s = await fetchJSON('/api/stats');
    $('a-total').textContent = s.total;
    $('a-open').textContent = s.total - s.resolved;
    $('a-high').textContent = s.high_priority_open;
    $('a-rate').textContent = s.resolution_rate + '%';
  }

  // Plain CSS bars: three simple distributions don't justify a chart library.
  function barRows(data, colorOf) {
    const max = Math.max(1, ...Object.values(data));
    return Object.entries(data).map(([label, n]) => `
      <div class="bar-row">
        <span class="bar-row__label">${escapeHTML(label)}</span>
        <div class="bar-row__track"><div style="width:${n / max * 100}%;background:${colorOf(label)};"></div></div>
        <span class="bar-row__n mono">${n}</span>
      </div>`).join('');
  }

  function trendColumns(trend) {
    const max = Math.max(1, ...trend.map(t => t.count));
    return `<div class="trend">${trend.map(t => `
      <div class="trend__col" title="${t.date}: ${t.count} new">
        <span class="trend__n mono">${t.count || ''}</span>
        <div class="trend__bar" style="height:${t.count / max * 100}%;"></div>
        <span class="trend__d mono">${t.date.slice(8)}</span>
      </div>`).join('')}</div>`;
  }

  async function loadAnalytics() {
    const a = await fetchJSON('/api/analytics');
    $('chart-category').innerHTML = barRows(a.by_category, () => 'var(--blue)');
    $('chart-status').innerHTML = barRows(a.by_status, (s) => STATUS_COLORS[s] || '#999');
    $('chart-trend').innerHTML = trendColumns(a.trend);
  }

  async function loadHotspots() {
    const clusters = await fetchJSON('/api/hotspots');
    $('hotspot-list').innerHTML = clusters.length ? clusters.slice(0, 6).map(c => `
      <div class="hotspot-row">
        <div>
          <div class="cat">${escapeHTML(c.area)}</div>
          <div class="coords">${Object.entries(c.categories).map(([k, n]) => `${escapeHTML(k)} ×${n}`).join(' · ')}</div>
        </div>
        <span class="count">${c.count} open</span>
      </div>`).join('')
      : empty('No hotspots — open issues are not clustered anywhere.');
  }

  // --------------------------------------------------------- review queue
  async function loadReviewQueue() {
    const rows = await fetchJSON('/api/reviews');
    $('review-queue-panel').style.display = rows.length ? 'block' : 'none';
    const photoBox = (url, label, fallback) => `
      <div class="review-card__photo-box">
        ${url ? `<img src="${url}" data-zoom="${url}">` : `<div class="review-card__nophoto">${fallback}</div>`}
        <div class="lbl">${label}</div>
      </div>`;

    $('review-queue-list').innerHTML = rows.map(issue => `
      <div class="review-card">
        <div class="review-card__head">
          <div><div class="review-card__title">${escapeHTML(issue.title)}</div>
            <div class="review-card__meta">${escapeHTML(issue.category)} · by <strong>${escapeHTML(issue.resolved_by_name)}</strong>
              (${escapeHTML(issue.resolved_by_skill || '')}) · ${timeAgo(issue.updated_at)} · ₹${issue.reward_amount.toFixed(0)} on hold</div></div>
          <span class="badge" style="background:#fff;color:#8a5b0f;border:1px solid var(--amber);">Score ${issue.verification_score}/100</span>
        </div>
        <div class="review-card__photos">
          ${photoBox(issue.photo_url, 'Original report', 'No original photo')}
          ${photoBox(issue.completion_photo_url, "Worker's proof", 'Missing')}
        </div>
        <div class="review-card__reasons"><strong>Why it was flagged:</strong> ${escapeHTML(issue.verification_notes || 'Unspecified')}</div>
        <div class="review-card__actions">
          <button class="btn btn-ghost btn-sm" data-reject="${issue.id}">Reject — worker must redo</button>
          <button class="btn btn-primary btn-sm" data-approve="${issue.id}">Approve &amp; pay</button>
        </div>
      </div>`).join('');

    const list = $('review-queue-list');
    list.querySelectorAll('[data-zoom]').forEach(img => img.addEventListener('click', () => window.open(img.dataset.zoom, '_blank')));
    list.querySelectorAll('[data-approve]').forEach(btn => btn.addEventListener('click', () =>
      act(btn, `/api/issues/${btn.dataset.approve}/approve-reward`, {}, 'Approved — reward paid to the worker.')));
    list.querySelectorAll('[data-reject]').forEach(btn => btn.addEventListener('click', () => {
      const note = prompt('Tell the worker what to fix (optional):');
      if (note === null) return;                       // cancelled
      act(btn, `/api/issues/${btn.dataset.reject}/reject-reward`, { note }, 'Sent back to the worker.');
    }));
  }

  async function loadPoliticianReview() {
    const rows = await fetchJSON('/api/politicians/pending');
    $('politician-review-panel').style.display = rows.length ? 'block' : 'none';
    $('politician-review-list').innerHTML = rows.map(user => `
      <div class="hotspot-row">
        <div>
          <div class="cat">${escapeHTML(user.name)} · ${escapeHTML(user.politician_title || 'Politician')}</div>
          <div class="coords">${escapeHTML(user.email)} · ${escapeHTML(user.politician_constituency || 'No constituency')}</div>
        </div>
        <div style="display:flex;gap:8px;">
          <button class="btn btn-ghost btn-sm" data-politician-reject="${user.id}">Reject</button>
          <button class="btn btn-primary btn-sm" data-politician-approve="${user.id}">Approve</button>
        </div>
      </div>`).join('');
    $('politician-review-list').querySelectorAll('[data-politician-approve],[data-politician-reject]')
      .forEach(button => button.addEventListener('click', async () => {
        const id = button.dataset.politicianApprove || button.dataset.politicianReject;
        const decision = button.hasAttribute('data-politician-approve') ? 'approve' : 'reject';
        try {
          await fetchJSON(`/api/politicians/${id}/review`, {
            method: 'POST', body: JSON.stringify({ decision }),
          });
          showToast(`Politician account ${decision === 'approve' ? 'approved' : 'rejected'}.`);
          loadPoliticianReview();
        } catch (error) { showToast(error.message); }
      }));
  }

  async function act(btn, url, body, okMessage) {
    btn.disabled = true;
    try {
      await fetchJSON(url, { method: 'POST', body: JSON.stringify(body) });
      showToast(okMessage);
      refreshAll();
    } catch (err) {
      showToast(err.message);
      btn.disabled = false;
    }
  }

  // -------------------------------------------------------------- rewards
  async function loadRewards() {
    const r = await fetchJSON('/api/rewards-summary');
    $('r-paid').textContent = money(r.paid);
    $('r-reserved').textContent = money(r.reserved);
    $('r-available').textContent = money(r.available);
    $('r-jobs').textContent = r.jobs_done;
    $('worker-leaderboard').innerHTML = r.leaderboard.length ? r.leaderboard.map((w, i) => `
      <div class="hotspot-row">
        <div><div class="cat">#${i + 1} ${escapeHTML(w.name)}</div>
          <div class="coords" style="font-family:var(--font-body);">${escapeHTML(w.skill || '')} · ${w.jobs} job${w.jobs === 1 ? '' : 's'}</div></div>
        <span class="count" style="background:var(--green-soft);color:var(--green);">${money(w.earned)}</span>
      </div>`).join('') : empty('No reward has been paid yet.');
  }

  async function loadSponsors() {
    const sponsors = await fetchJSON('/api/sponsors');
    const list = $('sponsor-list');
    list.innerHTML = sponsors.map(sp => {
      const pct = Math.max(0, Math.min(100, sp.budget_remaining / sp.budget_total * 100));
      return `
        <div class="sponsor-row ${sp.active ? '' : 'inactive'}">
          <div><div class="sp-name">${escapeHTML(sp.name)}</div>
            <div class="sp-meta">${escapeHTML(sp.category)} · ${money(sp.reward_per_issue)}/job · ${money(sp.budget_remaining)} of ${money(sp.budget_total)} free</div></div>
          <div style="display:flex;align-items:center;gap:10px;">
            <div class="sponsor-budget-bar"><div class="sponsor-budget-bar__fill" style="width:${pct}%;"></div></div>
            <button class="btn btn-ghost btn-sm" data-topup="${sp.id}">+ Top up</button>
            <button class="btn btn-ghost btn-sm" data-toggle="${sp.id}">${sp.active ? 'Pause' : 'Resume'}</button>
          </div>
        </div>`;
    }).join('');

    list.querySelectorAll('[data-toggle]').forEach(btn => btn.addEventListener('click', async () => {
      await fetchJSON(`/api/sponsors/${btn.dataset.toggle}/toggle`, { method: 'POST' });
      refreshAll();
    }));
    list.querySelectorAll('[data-topup]').forEach(btn => btn.addEventListener('click', async () => {
      const amount = Number(prompt("Top up this sponsor's budget by how many ₹?"));
      if (!amount || amount <= 0) return;
      try {
        await fetchJSON(`/api/sponsors/${btn.dataset.topup}/topup`, { method: 'POST', body: JSON.stringify({ amount }) });
        showToast('Budget topped up.');
        refreshAll();
      } catch (err) { showToast(err.message); }
    }));
  }

  $('form-add-sponsor').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await fetchJSON('/api/sponsors', { method: 'POST', body: JSON.stringify({
        name: $('sp-name').value.trim(), category: $('sp-category').value,
        reward_per_issue: $('sp-reward').value, budget_total: $('sp-budget').value }) });
      showToast('Sponsor added.');
      e.target.reset();
      refreshAll();
    } catch (err) { showToast(err.message); }
  });

  // --------------------------------------------------------- issues table
  function workerCell(i) {
    if (i.status === 'Resolved' && i.resolved_by_name) {
      const state = i.verification_status === 'pending_review'
        ? '<br><span style="color:var(--amber);">⚠️ payout awaiting review</span>'
        : i.reward_paid ? `<br>${money(i.reward_amount)} paid` : '';
      return `Resolved by <strong>${escapeHTML(i.resolved_by_name)}</strong>${state}`;
    }
    if (i.status === 'Claimed')
      return `Held by <strong>${escapeHTML(i.claimed_by_name)}</strong><br>${money(i.reward_amount)} reserved`;
    if (i.status === 'Reported')
      return i.estimated_reward > 0
        ? `<span style="color:var(--text-muted);">Open · ${money(i.estimated_reward)} reward</span>`
        : '<span style="color:var(--red);">No sponsor funding</span>';
    return '<span style="color:var(--text-muted);">—</span>';
  }

  // What an admin may do depends on the job: a worker-held job can only be taken back, and a
  // resolved one is final. This mirrors the rules the server enforces.
  function statusSelect(i) {
    if (i.status === 'Resolved') return `<select class="status-select" disabled><option>Resolved</option></select>`;
    const options = i.status === 'Claimed'
      ? [['Claimed', 'Claimed (worker)'], ['Reported', 'Take back → Reported']]
      : ADMIN_STATUSES.map(s => [s, s]);
    return `<select class="status-select" data-issue="${i.id}">${options.map(([v, l]) =>
      `<option value="${v}" ${v === i.status ? 'selected' : ''}>${l}</option>`).join('')}</select>`;
  }

  function lifecycleSelect(i, politicians) {
    const index = ADMIN_LIFECYCLE.indexOf(i.public_status);
    const next = index >= 0 ? ADMIN_LIFECYCLE[index + 1] : null;
    if (!next || next === 'Verified') return '';
    if (next === 'Assigned' && !i.concerned_politician_id) {
      return `<div style="display:flex;gap:4px;margin-top:4px;">
        <select data-assign-politician="${i.id}">
          <option value="">Assign politician…</option>
          ${politicians.map(p => `<option value="${p.id}">${escapeHTML(p.name)}</option>`).join('')}
        </select>
        <button class="btn btn-primary btn-sm" data-assign-advance="${i.id}" data-next="${next}" disabled>Assign</button>
      </div>`;
    }
    return `<select class="status-select" data-lifecycle="${i.id}">
      <option value="">${escapeHTML(i.public_status)}</option>
      <option value="${escapeHTML(next)}">Advance → ${escapeHTML(next)}</option>
    </select>`;
  }

  async function loadTable() {
    const params = new URLSearchParams();
    for (const k in filters) if (filters[k]) params.set(k, filters[k]);
    const [issues, politicians] = await Promise.all([
      fetchJSON('/api/issues?' + params),
      verifiedPoliticians || fetchJSON('/api/politicians'),
    ]);
    verifiedPoliticians = politicians;
    const tbody = $('issues-tbody');
    if (!issues.length) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:24px;">No issues match these filters.</td></tr>';
      return;
    }
    tbody.innerHTML = issues.map(i => `
      <tr>
        <td><strong>${escapeHTML(i.title)}</strong><br><span style="font-size:11px;color:var(--text-muted);">${escapeHTML(i.address || '')}</span></td>
        <td>${escapeHTML(i.category)}</td>
        <td><span class="badge badge-priority-${i.priority}">${i.priority}</span></td>
        <td class="mono">${i.impact_score < 0 ? '—' : i.impact_score}</td>
        <td class="mono">${i.upvotes}</td>
        <td>${timeAgo(i.created_at)}</td>
        <td style="font-size:11.5px;">${workerCell(i)}</td>
        <td><div>${escapeHTML(i.public_status)}</div>${lifecycleSelect(i, politicians)}${statusSelect(i)}</td>
      </tr>`).join('');

    tbody.querySelectorAll('select[data-issue]').forEach(sel => sel.addEventListener('change', async () => {
      try {
        await fetchJSON(`/api/issues/${sel.dataset.issue}/status`, { method: 'POST', body: JSON.stringify({ status: sel.value }) });
        showToast('Status updated.');
      } catch (err) { showToast(err.message); }
      refreshAll();                                    // also resets the dropdown if the server refused
    }));
    tbody.querySelectorAll('select[data-lifecycle]').forEach(sel => sel.addEventListener('change', async () => {
      if (!sel.value) return;
      try {
        await fetchJSON(`/api/issues/${sel.dataset.lifecycle}/lifecycle`, {
          method: 'POST', body: JSON.stringify({ status: sel.value }),
        });
        showToast(`Issue moved to ${sel.value}.`);
      } catch (error) { showToast(error.message); }
      refreshAll();
    }));
    tbody.querySelectorAll('select[data-assign-politician]').forEach(sel =>
      sel.addEventListener('change', () => {
        const button = tbody.querySelector(`[data-assign-advance="${sel.dataset.assignPolitician}"]`);
        button.disabled = !sel.value;
      }));
    tbody.querySelectorAll('[data-assign-advance]').forEach(button => button.addEventListener('click', async () => {
      const politician = tbody.querySelector(`[data-assign-politician="${button.dataset.assignAdvance}"]`);
      try {
        await fetchJSON(`/api/issues/${button.dataset.assignAdvance}/lifecycle`, {
          method: 'POST',
          body: JSON.stringify({
            status: button.dataset.next,
            concerned_politician_id: politician.value,
          }),
        });
        showToast(`Issue assigned and moved to ${button.dataset.next}.`);
      } catch (error) { showToast(error.message); }
      refreshAll();
    }));
  }

  const refreshAll = () => Promise.all([loadStats(), loadAnalytics(), loadHotspots(), loadReviewQueue(),
                                        loadPoliticianReview(), loadRewards(), loadSponsors(), loadTable()]);

  document.addEventListener('DOMContentLoaded', () => {
    $('t-status').addEventListener('change', (e) => { filters.status = e.target.value; loadTable(); });
    $('t-category').addEventListener('change', (e) => { filters.category = e.target.value; loadTable(); });
    $('t-search').addEventListener('input', debounce((e) => { filters.q = e.target.value.trim(); loadTable(); }, 300));
    refreshAll();
    connectLive(refreshAll);
  });
})();
