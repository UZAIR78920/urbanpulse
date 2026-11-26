(function () {
  const list = document.getElementById('politician-list');
  document.addEventListener('DOMContentLoaded', async () => {
    try {
      const politicians = await fetchJSON('/api/politicians');
      list.innerHTML = politicians.length ? politicians.map(p => `
        <article class="job-card">
          <div class="job-card__title">${escapeHTML(p.name)}</div>
          <div class="job-card__meta">${escapeHTML(p.politician_title || 'Politician')}
            ${p.politician_constituency ? ' · ' + escapeHTML(p.politician_constituency) : ''}</div>
          <div class="hint">${p.followers_count} followers · ${p.civic_xp} Civic XP</div>
          <div class="job-card__footer">
            <a class="btn btn-ghost btn-sm" href="/profile/${p.id}">View accountability profile</a>
            <button class="btn btn-primary btn-sm" data-follow="${p.id}">
              ${p.followed_by_me ? 'Following' : 'Follow'}
            </button>
          </div>
        </article>`).join('') : '<p class="empty-state">No verified politicians are listed yet.</p>';
      list.querySelectorAll('[data-follow]').forEach(button => button.addEventListener('click', async () => {
        try {
          const result = await fetchJSON(`/api/politicians/${button.dataset.follow}/follow`, { method: 'POST' });
          button.textContent = result.following ? 'Following' : 'Follow';
        } catch (error) { showToast(error.message); }
      }));
    } catch (error) {
      list.innerHTML = `<p class="form-error">${escapeHTML(error.message)}</p>`;
    }
  });
})();
