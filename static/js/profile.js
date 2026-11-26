(function () {
  const $ = (id) => document.getElementById(id);
  const renderBars = (items) => {
    const max = Math.max(1, ...items.map(item => item.count));
    return items.length ? items.map(item => `
      <div class="bar-row">
        <span class="bar-row__label">${escapeHTML(item.label)}</span>
        <div class="bar-row__track"><div style="width:${item.count / max * 100}%;background:var(--blue);"></div></div>
        <span class="bar-row__n mono">${item.count}</span>
      </div>`).join('') : '<p class="hint">No contributions yet.</p>';
  };
  document.addEventListener('DOMContentLoaded', async () => {
    try {
      const profile = await fetchJSON(`/api/profiles/${PROFILE.id}`);
      $('profile-monthly').innerHTML = renderBars(profile.monthly.map(row => ({
        label: row.month, count: row.count,
      })));
      $('profile-categories').innerHTML = renderBars(Object.entries(profile.categories).map(([label, count]) => ({
        label, count,
      })));
      const follow = $('btn-follow-politician');
      if (follow) {
        follow.textContent = profile.followed_by_me ? 'Following' : 'Follow';
        follow.addEventListener('click', async () => {
          try {
            const result = await fetchJSON(`/api/politicians/${PROFILE.id}/follow`, { method: 'POST' });
            follow.textContent = result.following ? 'Following' : 'Follow';
          } catch (error) { showToast(error.message); }
        });
      }
    } catch (error) { showToast(error.message); }
  });
})();
