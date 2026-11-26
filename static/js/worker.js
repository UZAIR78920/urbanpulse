// UrbanPulse — worker job board
(function () {
  const $ = (id) => document.getElementById(id);
  let category = '';
  let myLoc = null;              // {lat, lng, radius_km, label}
  let jobMaps = [];              // Leaflet maps on the "My jobs" cards
  let resolveId = null;
  let photo;

  const hasLoc = () => myLoc && myLoc.lat != null;
  const snippet = (t) => escapeHTML(t.length > 140 ? t.slice(0, 140) + '…' : t);

  function rewardTag(issue) {
    const amount = issue.reward_amount || issue.estimated_reward;
    return `<span class="reward-tag">${issue.reward_amount ? 'Reserved' : 'Reward'}: ₹${amount.toFixed(0)}</span>`;
  }

  function jobHeader(issue, extra) {
    return `<div class="job-card__top">
      <div><div class="job-card__title">${escapeHTML(issue.title)}</div>
        <div class="job-card__meta">${escapeHTML(issue.category)} · ${escapeHTML(extra)}</div></div>
      <span class="badge badge-priority-${issue.priority}">${issue.priority}</span></div>`;
  }

  // -------------------------------------------------------------- summary
  async function loadSummary() {
    const s = await fetchJSON('/api/worker/summary');
    $('w-wallet').textContent = '₹' + s.wallet_balance.toFixed(0);
    $('w-active').textContent = s.active_jobs;
    $('w-resolved').textContent = s.resolved_jobs;
    $('w-available').textContent = s.has_location ? s.available_jobs : '—';
  }

  // -------------------------------------------------------- work location
  function renderLocation() {
    $('loc-bar').classList.toggle('needs-location', !hasLoc());
    if (hasLoc()) {
      $('loc-value').textContent = (myLoc.label || `${myLoc.lat.toFixed(4)}, ${myLoc.lng.toFixed(4)}`) +
                                   ` · jobs within ${myLoc.radius_km} km`;
      $('loc-radius').value = String(myLoc.radius_km);
    } else {
      $('loc-value').textContent = 'Not set — set it to see jobs near you';
    }
  }

  function locMsg(text, cls) {
    $('loc-msg').textContent = text || '';
    $('loc-msg').className = 'loc-bar__msg' + (cls ? ' ' + cls : '');
  }

  async function saveLocation(patch) {
    myLoc = await fetchJSON('/api/worker/location', { method: 'POST', body: JSON.stringify(patch) });
    renderLocation();
    refreshAll();
  }

  function setupLocationBar() {
    $('btn-loc-me').addEventListener('click', async () => {
      const btn = $('btn-loc-me');
      btn.disabled = true;
      locMsg('Locating…');
      try {
        const loc = await getCurrentLocation();
        await saveLocation({ lat: loc.lat, lng: loc.lng, label: 'Current location' });
        locMsg(`Work area updated (±${Math.round(loc.accuracy)} m).`, 'ok');
      } catch (err) { locMsg(err.message, 'error'); }
      finally { btn.disabled = false; }
    });

    $('loc-radius').addEventListener('change', async (e) => {
      if (!hasLoc()) return locMsg('Set your location first.', 'error');
      try { await saveLocation({ radius_km: parseFloat(e.target.value) }); locMsg('Distance updated.', 'ok'); }
      catch (err) { locMsg(err.message, 'error'); }
    });

    const input = $('loc-search'), box = $('loc-search-results');
    const hide = () => { box.classList.remove('open'); box.innerHTML = ''; };
    let seq = 0;

    async function run(q, pickFirst) {
      q = q.trim();
      if (q.length < 2) return hide();
      const mine = ++seq;
      box.classList.add('open');
      box.innerHTML = '<div class="sr-note">Searching…</div>';
      let places;
      try { places = await searchPlaces(q, 5); }
      catch (e) { box.innerHTML = '<div class="sr-note">Place search needs an internet connection.</div>'; return; }
      if (mine !== seq) return;
      const choose = async (p) => {
        hide();
        input.value = p.name.split(',')[0];
        try {
          await saveLocation({ lat: p.lat, lng: p.lng, label: p.name.split(',').slice(0, 2).join(',').trim() });
          locMsg('Work area updated.', 'ok');
        } catch (err) { locMsg(err.message, 'error'); }
      };
      if (pickFirst && places.length) return choose(places[0]);
      if (!places.length) { box.innerHTML = '<div class="sr-note">No places found.</div>'; return; }
      box.innerHTML = places.map((p, i) => `<div class="sr-item" data-i="${i}">📍 ${escapeHTML(p.name)}</div>`).join('');
      box.querySelectorAll('[data-i]').forEach(el => el.addEventListener('click', () => choose(places[+el.dataset.i])));
    }
    const debounced = debounce((v) => run(v, false), 450);
    input.addEventListener('input', () => debounced(input.value));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); run(input.value, true); }
      if (e.key === 'Escape') hide();
    });
    document.addEventListener('click', (e) => { if (!e.target.closest('.search-box')) hide(); });
  }

  // ------------------------------------------------------------ job board
  async function loadAvailable() {
    const grid = $('available-grid'), note = $('funding-note');
    const data = await fetchJSON('/api/worker/jobs' + (category ? '?category=' + encodeURIComponent(category) : ''));
    note.style.display = 'none';
    if (!data.has_location) {
      grid.innerHTML = '<div class="empty-state">📍 Set your work area above to see jobs near you.</div>';
      return;
    }
    if (data.waiting_for_funding) {
      note.textContent = `${data.waiting_for_funding} more job(s) nearby are waiting for a sponsor to fund their category.`;
      note.style.display = 'block';
    }
    if (!data.jobs.length) {
      grid.innerHTML = `<div class="empty-state">No open jobs for you within ${myLoc.radius_km} km right now. Try a larger distance or check back soon.</div>`;
      return;
    }
    grid.innerHTML = data.jobs.map(issue => `
      <div class="job-card">
        ${jobHeader(issue, issue.address || 'No landmark given')}
        <div class="job-card__desc">${snippet(issue.description)}</div>
        <div><span class="distance-tag">📍 ${formatKm(issue.distance_km)} away</span></div>
        <div class="job-card__footer">${rewardTag(issue)}
          <button class="btn btn-primary btn-sm" data-claim="${issue.id}">Claim job</button></div>
      </div>`).join('');
    grid.querySelectorAll('[data-claim]').forEach(b => b.addEventListener('click', () => claimJob(b.dataset.claim)));
  }

  async function claimJob(id) {
    try {
      await fetchJSON(`/api/issues/${id}/claim`, { method: 'POST' });
      showToast("Job claimed — it's locked to you and its reward is reserved.");
      await refreshAll();
      document.querySelector('.worker-tabs [data-panel="panel-mine"]').click();
    } catch (err) {
      showToast(err.message);
      refreshAll();
    }
  }

  // ------------------------------------------------------------- my jobs
  async function loadMine() {
    const grid = $('mine-grid');
    const issues = await fetchJSON('/api/worker/my-jobs');
    jobMaps.forEach(m => m.remove());
    jobMaps = [];
    if (!issues.length) {
      grid.innerHTML = "<div class=\"empty-state\">You aren't holding any jobs — claim one from the Available Jobs tab.</div>";
      return;
    }
    grid.innerHTML = issues.map(issue => `
      <div class="job-card">
        ${jobHeader(issue, issue.address || 'No landmark given')}
        ${issue.verification_status === 'rejected' ? `<div class="form-error" style="margin:0;">Ward office asked you to redo the photo: ${escapeHTML(issue.verification_notes)}</div>` : ''}
        <div class="job-map" id="job-map-${issue.id}"></div>
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
          <span class="job-coords">📍 ${issue.lat.toFixed(5)}, ${issue.lng.toFixed(5)}${issue.distance_km != null ? ' · ' + formatKm(issue.distance_km) + ' from you' : ''}</span>
          <a class="btn btn-ghost btn-sm" target="_blank" rel="noopener"
             href="https://www.google.com/maps/dir/?api=1&destination=${issue.lat},${issue.lng}">🧭 Navigate</a>
        </div>
        <div class="job-card__desc">${snippet(issue.description)}</div>
        <div class="job-card__footer">${rewardTag(issue)}
          <div style="display:flex;gap:8px;">
            ${issue.public_status === 'Assigned' ? `<button class="btn btn-ghost btn-sm" data-start="${issue.id}">Start work</button>` : ''}
            <button class="btn btn-ghost btn-sm" data-release="${issue.id}">Release</button>
            <button class="btn btn-amber btn-sm" data-resolve="${issue.id}">Submit work</button>
          </div></div>
      </div>`).join('');

    grid.querySelectorAll('[data-release]').forEach(b => b.addEventListener('click', () => releaseJob(b.dataset.release)));
    grid.querySelectorAll('[data-start]').forEach(b => b.addEventListener('click', () => startJob(b.dataset.start)));
    grid.querySelectorAll('[data-resolve]').forEach(b => b.addEventListener('click', () => openResolve(b.dataset.resolve)));

    issues.forEach(issue => {
      const m = L.map('job-map-' + issue.id, { scrollWheelZoom: false }).setView([issue.lat, issue.lng], 16);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; OpenStreetMap', maxZoom: 19 }).addTo(m);
      L.marker([issue.lat, issue.lng]).addTo(m).bindPopup(escapeHTML(issue.title));
      if (hasLoc()) {
        L.circleMarker([myLoc.lat, myLoc.lng], { radius: 7, color: '#fff', weight: 2, fillColor: '#2563EB', fillOpacity: 1 })
          .addTo(m).bindPopup('Your work area');
        m.fitBounds([[issue.lat, issue.lng], [myLoc.lat, myLoc.lng]], { padding: [30, 30], maxZoom: 16 });
      }
      jobMaps.push(m);
    });
  }

  async function releaseJob(id) {
    if (!confirm('Release this job? Its reward goes back to the sponsor and the job re-opens for others.')) return;
    try {
      await fetchJSON(`/api/issues/${id}/release`, { method: 'POST' });
      showToast('Job released.');
      refreshAll();
    } catch (err) { showToast(err.message); }
  }

  async function startJob(id) {
    try {
      await fetchJSON(`/api/issues/${id}/lifecycle`, {
        method: 'POST', body: JSON.stringify({ status: 'In Progress' }),
      });
      showToast('Work started.');
      refreshAll();
    } catch (error) { showToast(error.message); }
  }

  // --------------------------------------------------------- submit work
  function openResolve(id) {
    resolveId = id;
    $('resolve-note').value = '';
    photo.clear();
    $('modal-resolve').classList.add('open');
  }

  async function submitResolve(e) {
    e.preventDefault();
    if (!photo.get()) return showToast('Please attach a photo of the completed work first.');
    const btn = $('btn-submit-resolve');
    const fd = new FormData();
    fd.append('note', $('resolve-note').value.trim());
    fd.append('photo', photo.get(), 'proof.jpg');
    btn.disabled = true;
    btn.textContent = 'Submitting…';
    try {
      const issue = await fetchJSON(`/api/issues/${resolveId}/resolve`, { method: 'POST', body: fd });
      $('modal-resolve').classList.remove('open');
      showToast(issue.verification_status === 'auto_approved'
        ? `✅ Photo verified — ₹${issue.reward_amount.toFixed(0)} added to your earnings.`
        : "📋 Sent for ward review — you'll be paid once the photo is approved.");
      refreshAll();
    } catch (err) { showToast(err.message); }
    finally { btn.disabled = false; btn.textContent = 'Submit & request reward'; }
  }

  async function loadPending() {
    const rows = await fetchJSON('/api/worker/pending-review');
    $('pending-review-section').style.display = rows.length ? 'block' : 'none';
    $('pending-review-grid').innerHTML = rows.map(issue => `
      <div class="job-card">
        ${jobHeader(issue, 'submitted ' + timeAgo(issue.updated_at))}
        <div class="job-card__desc">The automated check flagged: ${escapeHTML(issue.verification_notes)}</div>
        <div class="job-card__footer">${rewardTag(issue)}
          <span style="font-size:11.5px;color:var(--text-muted);">A ward admin will review your photo.</span></div>
      </div>`).join('');
  }

  async function loadHistory() {
    const rows = await fetchJSON('/api/worker/history');
    $('history-list').innerHTML = rows.length ? rows.map(r => `
      <div class="history-row">
        <div><div class="ht">${escapeHTML(r.title)}</div>
          <div class="hm">${escapeHTML(r.category)} · via ${escapeHTML(r.sponsor_name)} · ${timeAgo(r.created_at)}</div></div>
        <div class="hr">+₹${r.amount.toFixed(0)}</div>
      </div>`).join('')
      : '<p style="font-size:13px;color:var(--text-muted);">No rewards yet — complete a job to start earning.</p>';
  }

  const refreshAll = () => Promise.all([loadSummary(), loadAvailable(), loadMine(), loadPending(), loadHistory()]);

  // ----------------------------------------------------------------- boot
  document.addEventListener('DOMContentLoaded', async () => {
    document.querySelectorAll('.worker-tabs button').forEach(btn => btn.addEventListener('click', () => {
      document.querySelectorAll('.worker-tabs button, .worker-panel').forEach(x => x.classList.remove('active'));
      btn.classList.add('active');
      $(btn.dataset.panel).classList.add('active');
      setTimeout(() => jobMaps.forEach(m => m.invalidateSize()), 50);   // maps were hidden when drawn
    }));

    const chips = $('w-category-filter');
    chips.addEventListener('click', (e) => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      [...chips.children].forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      category = chip.dataset.value;
      loadAvailable();
    });

    photo = setupPhotoPicker({ input: 'resolve-photo-input', button: 'btn-resolve-photo', preview: 'resolve-photo-preview',
                               previewImg: 'resolve-photo-preview-img', removeButton: 'btn-resolve-remove-photo' });
    $('form-resolve').addEventListener('submit', submitResolve);
    document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click',
      () => $('modal-resolve').classList.remove('open')));
    $('modal-resolve').addEventListener('click', (e) => { if (e.target.id === 'modal-resolve') e.target.classList.remove('open'); });

    setupLocationBar();
    try { myLoc = await fetchJSON('/api/worker/location'); } catch (e) { myLoc = null; }
    renderLocation();
    refreshAll();
    connectLive(refreshAll);
  });
})();
