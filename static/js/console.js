// UrbanPulse — citizen console: live map, reporting, issue detail
(function () {
  const $ = (id) => document.getElementById(id);
  const MAX_MEDIA_FILES = 5;
  const state = { status: '', category_group: '', mine: false };
  const markers = {};            // issue id -> Leaflet marker
  let allIssues = [];
  let map, searchMarker;
  let pickMap, pickMarker, pickLatLng = null;
  let selectedMedia = [];

  // ------------------------------------------------------------------ map
  const TILES = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
  const ATTRIBUTION = '&copy; OpenStreetMap contributors';

  function initMap() {
    map = L.map('map').setView(window.URBANPULSE.center, 13);
    L.tileLayer(TILES, { attribution: ATTRIBUTION, maxZoom: 19 }).addTo(map);
  }

  function pinIcon(status) {
    return L.divIcon({ className: '', html: `<div class="up-pin p-${statusSlug(status)}"></div>`,
                       iconSize: [16, 16], iconAnchor: [8, 14] });
  }

  function drawMarkers(issues) {
    Object.values(markers).forEach(m => map.removeLayer(m));
    for (const id in markers) delete markers[id];
    issues.forEach(issue => {
      const m = L.marker([issue.lat, issue.lng], { icon: pinIcon(issue.status) }).addTo(map);
      m.on('click', () => openDetail(issue.id));
      markers[issue.id] = m;
    });
  }

  // --------------------------------------------------------------- data
  async function loadIssues() {
    const params = new URLSearchParams();
    if (state.status) params.set('status', state.status);
    if (state.category_group) params.set('category_group', state.category_group);
    if (state.mine) params.set('mine', '1');
    allIssues = await fetchJSON('/api/issues?' + params);
    if (map) drawMarkers(allIssues);
  }

  async function loadPopular() {
    const items = await fetchJSON('/api/issues?sort=popular');
    const list = $('popular-list');
    list.innerHTML = items.length ? items.slice(0, 6).map(issue => `
      <div class="feed-item" data-popular="${issue.id}">
        <div class="t">${escapeHTML(issue.title)}</div>
        <div class="m"><span>${escapeHTML(issue.category_group || issue.category)} · ${escapeHTML(issue.public_status)}</span>
          <span>▲ ${issue.upvotes} · ${issue.comments_count} comments</span></div>
      </div>`).join('') : '<div class="feed-empty">No civic issues yet.</div>';
    list.querySelectorAll('[data-popular]').forEach(item =>
      item.addEventListener('click', () => openDetail(+item.dataset.popular)));
  }

  async function loadStats() {
    const s = await fetchJSON('/api/stats');
    $('s-total').textContent = s.total;
    $('s-reported').textContent = s.reported;
    $('s-claimed').textContent = s.claimed;
    $('s-progress').textContent = s.in_progress;
    $('s-resolved').textContent = s.resolved;
  }

  const refreshAll = () => { loadIssues(); loadStats(); loadPopular(); };

  function setupFilters() {
    for (const [id, key] of [['f-status', 'status'], ['f-category', 'category_group']]) {
      const el = $(id);
      el.addEventListener('click', (e) => {
        const chip = e.target.closest('.chip');
        if (!chip) return;
        [...el.children].forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        state[key] = chip.dataset.value;
        loadIssues();
      });
    }
    const link = $('nav-my-reports');
    if (link) link.addEventListener('click', (e) => {
      e.preventDefault();
      state.mine = !state.mine;
      link.classList.toggle('active', state.mine);
      loadIssues();
      showToast(state.mine ? 'Showing only your reports' : 'Showing all issues');
    });
  }

  // --------------------------------------------------------------- search
  function flyToPlace(p) {
    if (p.bbox && p.bbox.length === 4) map.fitBounds([[p.bbox[0], p.bbox[2]], [p.bbox[1], p.bbox[3]]], { maxZoom: 15 });
    else map.setView([p.lat, p.lng], 14);
    if (searchMarker) map.removeLayer(searchMarker);
    searchMarker = L.circleMarker([p.lat, p.lng], { radius: 9, color: '#1C2430', weight: 3, fillColor: '#E0912B', fillOpacity: 0.9 })
      .addTo(map).bindPopup(escapeHTML(p.name.split(',').slice(0, 2).join(','))).openPopup();
  }

  function setupSearch() {
    const input = $('f-search'), box = $('search-results');
    let seq = 0;
    const hide = () => { box.classList.remove('open'); box.innerHTML = ''; };

    async function run(q, goToFirst) {
      q = q.trim();
      if (q.length < 2) return hide();
      const mine = ++seq;
      const needle = q.toLowerCase();
      const reports = allIssues.filter(i => `${i.title} ${i.address || ''} ${i.category}`.toLowerCase().includes(needle)).slice(0, 4);
      box.classList.add('open');
      box.innerHTML = '<div class="sr-note">Searching…</div>';

      let places = [], failed = false;
      try { places = await searchPlaces(q, 5); } catch (e) { failed = true; }
      if (mine !== seq) return;                       // a newer keystroke superseded this search
      if (goToFirst && places.length) { flyToPlace(places[0]); return hide(); }

      let html = '';
      if (places.length) html += '<div class="sr-head">Places</div>' + places.map((p, i) =>
        `<div class="sr-item" data-place="${i}">📍 ${escapeHTML(p.name)}</div>`).join('');
      if (reports.length) html += '<div class="sr-head">Reports</div>' + reports.map(r =>
        `<div class="sr-item" data-report="${r.id}">🛠 ${escapeHTML(r.title)}<small>${escapeHTML(r.address || r.category)}</small></div>`).join('');
      box.innerHTML = html || `<div class="sr-note">${failed ? 'Place search needs an internet connection.' : 'Nothing found.'}</div>`;

      box.querySelectorAll('[data-place]').forEach(el => el.addEventListener('click', () => {
        const p = places[+el.dataset.place];
        input.value = p.name.split(',')[0];
        flyToPlace(p); hide();
      }));
      box.querySelectorAll('[data-report]').forEach(el => el.addEventListener('click', () => {
        const issue = allIssues.find(i => i.id === +el.dataset.report);
        if (issue) map.setView([issue.lat, issue.lng], 17);
        hide(); openDetail(+el.dataset.report);
      }));
    }

    const debounced = debounce((v) => run(v, false), 450);
    input.addEventListener('input', () => debounced(input.value));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); run(input.value, true); }
      if (e.key === 'Escape') hide();
    });
    document.addEventListener('click', (e) => { if (!e.target.closest('.search-box')) hide(); });
  }

  // ------------------------------------------------------------ live feed
  function addFeedItem(evt) {
    // Votes change the counter, not the story — keep them out of the activity feed.
    if (evt.type !== 'new_issue' && evt.type !== 'issue_updated') return;
    const issue = evt.payload;
    const list = $('feed-list');
    list.querySelector('.feed-empty')?.remove();
    const div = document.createElement('div');
    div.className = `feed-item priority-${issue.priority}`;
    div.innerHTML = `<div class="t">${escapeHTML(issue.title)}</div>
      <div class="m"><span>${escapeHTML(issue.category)} · ${evt.type === 'new_issue' ? 'New report' : 'Now ' + issue.lifecycle_status}</span>
      <span>${timeAgo(issue.updated_at)}</span></div>`;
    div.addEventListener('click', () => openDetail(issue.id));
    list.prepend(div);
    while (list.children.length > 30) list.removeChild(list.lastChild);
  }

  // ---------------------------------------------------------- report form
  function openReportModal() {
    $('modal-report').classList.add('open');
    if (!pickMap) {
      pickMap = L.map('pick-map').setView(window.URBANPULSE.center, 13);
      L.tileLayer(TILES, { attribution: ATTRIBUTION }).addTo(pickMap);
      pickMap.on('click', (e) => setPicked(e.latlng.lat, e.latlng.lng, false));
    }
    setTimeout(() => pickMap.invalidateSize(), 60);   // the map was hidden when created
  }

  function setupReportForm() {
    const group = $('r-category-group');
    const subcategory = $('r-subcategory');
    const mediaInput = $('r-media-input');
    const preview = $('media-preview');
    const suggestionBox = $('ml-suggestion');
    const suggestionText = $('ml-suggestion-text');
    const duplicates = $('duplicate-candidates');

    function syncSubcategories(preferred) {
      const options = window.URBANPULSE.subcategories[group.value] || [];
      subcategory.innerHTML = options.map(value =>
        `<option value="${escapeHTML(value)}">${escapeHTML(value)}</option>`).join('');
      if (preferred && options.includes(preferred)) subcategory.value = preferred;
    }
    const unreviewed = () => { $('r-classification-reviewed').checked = false; };
    group.addEventListener('change', () => { syncSubcategories(); unreviewed(); });
    subcategory.addEventListener('change', unreviewed);
    $('r-severity').addEventListener('change', unreviewed);
    syncSubcategories();
    $('r-reported-date').value = new Date().toISOString().slice(0, 10);

    mediaInput.addEventListener('change', () => {
      selectedMedia = [...mediaInput.files];
      if (selectedMedia.length > MAX_MEDIA_FILES) {
        selectedMedia = [];
        mediaInput.value = '';
        preview.textContent = `Choose at most ${MAX_MEDIA_FILES} files.`;
        return;
      }
      preview.textContent = selectedMedia.map(file => file.name).join(' · ');
    });

    const suggest = debounce(async () => {
      const title = $('r-title').value.trim(), description = $('r-desc').value.trim();
      unreviewed();
      if (title.length + description.length < 8) {
        suggestionBox.style.display = 'none';
        return;
      }
      try {
        const result = await fetchJSON('/api/classify', {
          method: 'POST',
          body: JSON.stringify({
            title, description,
            lat: pickLatLng && pickLatLng.lat, lng: pickLatLng && pickLatLng.lng,
          }),
        });
        group.value = result.category_group;
        syncSubcategories(result.subcategory);
        $('r-severity').value = result.severity;
        suggestionText.textContent = `${result.category_group} / ${result.subcategory} · ${result.severity} severity · model margin ${Math.round(result.confidence * 100)}% (not a calibrated probability).`;
        duplicates.innerHTML = result.duplicate_candidates.length
          ? 'Possible nearby duplicates: ' + result.duplicate_candidates.map(item =>
            `<a href="#" data-duplicate="${item.id}">${escapeHTML(item.title)} (${Math.round(item.similarity * 100)}% text similarity)</a>`
          ).join(' · ')
          : '';
        duplicates.querySelectorAll('[data-duplicate]').forEach(link => link.addEventListener('click', event => {
          event.preventDefault();
          closeModals();
          openDetail(+link.dataset.duplicate);
        }));
        suggestionBox.style.display = 'block';
      } catch (error) {
        suggestionBox.style.display = 'block';
        suggestionText.textContent = `Classification unavailable: ${error.message}`;
        duplicates.textContent = '';
      }
    }, 500);
    $('r-title').addEventListener('input', suggest);
    $('r-desc').addEventListener('input', suggest);

    fetchJSON('/api/politicians').then(politicians => {
      $('r-politician').insertAdjacentHTML('beforeend', politicians.map(p =>
        `<option value="${p.id}">${escapeHTML(p.name)}${p.politician_constituency ? ' · ' + escapeHTML(p.politician_constituency) : ''}</option>`
      ).join(''));
    }).catch(error => showToast(error.message));
  }

  function setPicked(lat, lng, pan) {
    pickLatLng = { lat, lng };
    $('pick-coords').textContent = `Selected: ${lat.toFixed(5)}, ${lng.toFixed(5)}`;
    if (pickMarker) pickMap.removeLayer(pickMarker);
    pickMarker = L.marker([lat, lng]).addTo(pickMap);
    if (pan) pickMap.setView([lat, lng], 16);
  }

  async function useCurrentLocation() {
    const status = $('geo-status'), btn = $('btn-use-location');
    btn.disabled = true;
    status.className = 'hint';
    status.textContent = 'Locating…';
    try {
      const loc = await getCurrentLocation();
      setPicked(loc.lat, loc.lng, true);
      status.textContent = `Location set (±${Math.round(loc.accuracy)} m).`;
      status.className = 'hint ok';
    } catch (err) {
      status.textContent = err.message + ' You can tap the map instead.';
      status.className = 'hint error';
    } finally {
      btn.disabled = false;
    }
  }

  function closeModals() {
    document.querySelectorAll('.modal-backdrop').forEach(m => m.classList.remove('open'));
  }

  async function submitReport(e) {
    e.preventDefault();
    if (!pickLatLng) return showToast('Please set a location first — use your current location or tap the map.');
    const form = e.target, btn = $('btn-submit-report');
    const fd = new FormData();
    for (const f of ['title', 'address', 'description', 'reported_date']) fd.append(f, form[f].value.trim());
    fd.append('category_group', $('r-category-group').value);
    fd.append('subcategory', $('r-subcategory').value);
    fd.append('category', $('r-subcategory').value);
    fd.append('severity', $('r-severity').value);
    fd.append('concerned_politician_id', $('r-politician').value);
    fd.append('lat', pickLatLng.lat);
    fd.append('lng', pickLatLng.lng);
    for (const file of selectedMedia) {
      const upload = file.type.startsWith('image/') ? await shrinkPhoto(file) : file;
      fd.append('media', upload, file.name);
    }

    btn.disabled = true;
    btn.textContent = 'Submitting…';
    try {
      await fetchJSON('/api/issues', { method: 'POST', body: fd });
      showToast('Issue reported — thank you!');
      form.reset();
      selectedMedia = [];
      $('r-media-input').value = '';
      $('media-preview').textContent = '';
      $('r-politician').value = '';
      $('r-category-group').dispatchEvent(new Event('change'));
      $('ml-suggestion').style.display = 'none';
      setupReportDate();
      pickLatLng = null;
      if (pickMarker) { pickMap.removeLayer(pickMarker); pickMarker = null; }
      $('pick-coords').textContent = 'Use your location, or tap the map to drop a pin.';
      $('geo-status').textContent = '';
      closeModals();
      refreshAll();
    } catch (err) {
      showToast(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Submit report';
    }
  }

  function setupReportDate() {
    $('r-reported-date').value = new Date().toISOString().slice(0, 10);
  }

  // --------------------------------------------------------- issue detail
  function rewardLine(issue) {
    if (issue.status === 'Reported' && issue.estimated_reward > 0)
      return `<span class="reward-tag">Reward available: ₹${issue.estimated_reward.toFixed(0)}</span>`;
    if (issue.status === 'Claimed')
      return `<span class="reward-tag">₹${issue.reward_amount.toFixed(0)} reserved for the worker</span>`;
    if (issue.status === 'Resolved' && issue.verification_status === 'pending_review')
      return '<span class="reward-tag pending">Reward on hold — ward office reviewing the proof photo</span>';
    if (issue.status === 'Resolved' && issue.reward_paid)
      return `<span class="reward-tag">₹${issue.reward_amount.toFixed(0)} reward paid</span>`;
    return '';
  }

  async function openDetail(issueId) {
    const content = $('modal-detail-content');
    content.innerHTML = '<p>Loading…</p>';
    $('modal-detail').classList.add('open');
    const issue = await fetchJSON('/api/issues/' + issueId);
    const comments = await fetchJSON(`/api/issues/${issueId}/comments`);

    const own = issue.reporter_id === window.URBANPULSE.userId;
    const canVote = !own && issue.status !== 'Resolved';
    const canVerify = own && issue.public_status === 'Resolved';
    const lifecycleIndex = window.URBANPULSE.lifecycle.indexOf(issue.public_status);
    const nextLifecycle = window.URBANPULSE.lifecycle[lifecycleIndex + 1];
    const canAdvance = (window.URBANPULSE.role === 'admin'
      || (window.URBANPULSE.role === 'politician'
        && (issue.concerned_politician_id === null || issue.concerned_politician_id === window.URBANPULSE.userId)))
      && nextLifecycle && nextLifecycle !== 'Verified';
    const dim = 'font-size:12.5px;color:var(--text-muted);margin:0 0 10px;';
    const mediaHTML = (issue.media || []).map((media, index) => media.type === 'video'
      ? `<video class="issue-detail__photo" controls preload="metadata" src="${media.url}"></video>`
      : `<img class="issue-detail__photo" src="${media.url}" alt="Issue attachment ${index + 1}">`).join('');
    const lifecycleAction = canVerify
      ? '<button class="btn btn-primary btn-sm" id="btn-verify-issue">Verify resolution</button>'
      : canAdvance
        ? `<button class="btn btn-primary btn-sm" id="btn-next-lifecycle">Move to ${escapeHTML(nextLifecycle)}</button>`
        : '';

    content.innerHTML = `
      <div class="modal__head">
        <div><h2>${escapeHTML(issue.title)}</h2>
          <p class="sub" style="margin:4px 0 0;font-size:12.5px;">Reported by ${escapeHTML(issue.reporter_name)} · ${timeAgo(issue.created_at)}</p></div>
        <button class="modal__close" data-close>&times;</button>
      </div>
      <div class="issue-detail__meta">
        ${badgeHTML(issue.public_status, issue.severity)}
        <span class="badge" style="background:var(--line-soft);color:var(--text-primary);">${escapeHTML(issue.category_group || '')}</span>
        <span class="badge" style="background:var(--line-soft);color:var(--text-primary);">${escapeHTML(issue.category)}</span>
      </div>
      <p class="issue-detail__desc">${escapeHTML(issue.description)}</p>
      ${mediaHTML || (issue.photo_url ? `<img class="issue-detail__photo" src="${issue.photo_url}" alt="Photo of the reported issue">` : '')}
      ${issue.address ? `<p style="${dim}">📍 ${escapeHTML(issue.address)}</p>` : ''}
      <p style="${dim}">Reported ${escapeHTML(issue.reported_date || issue.created_at.slice(0, 10))} · Severity: ${escapeHTML(issue.severity)}</p>
      ${issue.politician_name ? `<p style="${dim}">Concerned politician: <a href="/profile/${issue.concerned_politician_id}">${escapeHTML(issue.politician_name)}</a></p>` : ''}
      ${issue.status === 'Claimed' ? `<p style="${dim}color:var(--teal);">🔧 Being fixed by ${escapeHTML(issue.claimed_by_name)} (${escapeHTML(issue.claimed_by_skill)})</p>` : ''}
      ${issue.status === 'Resolved' && issue.resolved_by_name ? `<p style="${dim}color:var(--green);">✅ Resolved by ${escapeHTML(issue.resolved_by_name)} (${escapeHTML(issue.resolved_by_skill)})${issue.resolution_note ? ' — “' + escapeHTML(issue.resolution_note) + '”' : ''}</p>` : ''}
      ${issue.completion_photo_url && issue.status === 'Resolved' ? `<img class="issue-detail__photo" src="${issue.completion_photo_url}" alt="Photo of the completed work" id="detail-proof">` : ''}
      <p style="margin:0 0 10px;">${rewardLine(issue)}</p>
      <div class="issue-detail__row">
        <button class="btn btn-ghost btn-sm" id="btn-upvote" ${canVote ? '' : 'disabled'}>▲ ${issue.upvotes}</button>
        <button class="btn btn-ghost btn-sm" id="btn-downvote" ${canVote ? '' : 'disabled'}>▼ ${issue.downvotes}</button>
        <button class="btn btn-ghost btn-sm" id="btn-follow-issue">${issue.followed_by_me ? 'Following issue' : 'Follow issue'}</button>
        <span class="hint mono" title="Severity + votes + waiting time">Impact score: ${issue.impact_score}</span>
        ${lifecycleAction}
      </div>
      <h3 style="font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-muted);margin-top:18px;">Status timeline</h3>
      <div class="timeline">${issue.timeline.map(t => `
        <div class="timeline-item"><div class="st">${escapeHTML(t.status)}</div>
          ${t.note ? `<div class="nt">${escapeHTML(t.note)}</div>` : ''}
          <div class="tm">${timeAgo(t.changed_at)}</div></div>`).join('')}
      </div>
      <h3 style="font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-muted);margin-top:18px;">Comments (${comments.length})</h3>
      <div id="issue-comments">${comments.map(comment => `
        <div class="timeline-item"><strong>${escapeHTML(comment.name)}</strong>
          <p>${escapeHTML(comment.body)}</p><span class="tm">${timeAgo(comment.created_at)}</span></div>`).join('') || '<p class="hint">No comments yet.</p>'}
      </div>
      <form id="form-comment" class="field">
        <label for="comment-body">Add a comment</label>
        <textarea id="comment-body" rows="2" maxlength="2000" required></textarea>
        <button class="btn btn-ghost btn-sm" type="submit">Comment</button>
      </form>`;

    content.querySelector('[data-close]').addEventListener('click', closeModals);

    async function vote(direction) {
      try {
        await fetchJSON(`/api/issues/${issueId}/vote`, { method: 'POST', body: JSON.stringify({ direction }) });
        await openDetail(issueId);
        refreshAll();
      } catch (err) { showToast(err.message); }
    }
    $('btn-upvote').addEventListener('click', () => vote('up'));
    $('btn-downvote').addEventListener('click', () => vote('down'));
    $('btn-follow-issue').addEventListener('click', async () => {
      try {
        const result = await fetchJSON(`/api/issues/${issueId}/follow`, { method: 'POST' });
        $('btn-follow-issue').textContent = result.following ? 'Following issue' : 'Follow issue';
      } catch (error) { showToast(error.message); }
    });
    $('form-comment').addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        await fetchJSON(`/api/issues/${issueId}/comments`, {
          method: 'POST', body: JSON.stringify({ body: $('comment-body').value }),
        });
        await openDetail(issueId);
        refreshAll();
      } catch (error) { showToast(error.message); }
    });
    const lifecycleButton = $('btn-next-lifecycle') || $('btn-verify-issue');
    if (lifecycleButton) lifecycleButton.addEventListener('click', async () => {
      const target = lifecycleButton.id === 'btn-verify-issue' ? 'Verified' : nextLifecycle;
      try {
        await fetchJSON(`/api/issues/${issueId}/lifecycle`, {
          method: 'POST', body: JSON.stringify({ status: target }),
        });
        showToast(`Issue moved to ${target}.`);
        await openDetail(issueId);
        refreshAll();
      } catch (error) { showToast(error.message); }
    });
  }

  // ----------------------------------------------------------------- boot
  document.addEventListener('DOMContentLoaded', () => {
    initMap();
    setupFilters();
    setupSearch();
    refreshAll();
    connectLive(refreshAll, { onEach: addFeedItem });

    setupReportForm();
    $('btn-report')?.addEventListener('click', openReportModal);
    $('form-report').addEventListener('submit', submitReport);
    $('btn-use-location').addEventListener('click', useCurrentLocation);
    document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', closeModals));
    document.querySelectorAll('.modal-backdrop').forEach(bd =>
      bd.addEventListener('click', (e) => { if (e.target === bd) closeModals(); }));
  });
})();
