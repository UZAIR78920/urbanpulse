// UrbanPulse — helpers shared by every page

const CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]')?.content || '';

function showToast(message) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove('show'), 3200);
}

function escapeHTML(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : str;
  return div.innerHTML;
}

function statusSlug(status) { return status.replace(/\s+/g, '-'); }

function badgeHTML(status, priority) {
  let html = `<span class="badge badge-status-${statusSlug(status)}">${status}</span>`;
  if (priority) html += ` <span class="badge badge-priority-${priority}">${priority} priority</span>`;
  return html;
}

function timeAgo(iso) {
  const then = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));   // server stores UTC
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

function formatKm(km) {
  return km == null ? '' : km < 1 ? Math.round(km * 1000) + ' m' : km.toFixed(1) + ' km';
}

function debounce(fn, wait) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), wait); };
}

// JSON / FormData requests. Sends the CSRF token on every call. FormData must NOT get a
// manual Content-Type: the browser has to add its own multipart boundary.
async function fetchJSON(url, options = {}) {
  const headers = { 'X-CSRF-Token': CSRF_TOKEN };
  if (!(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const res = await fetch(url, { ...options, headers });
  if (!res.ok) {
    let msg = 'Request failed';
    try { msg = (await res.json()).error || msg; } catch (e) { /* non-JSON error page */ }
    throw new Error(msg);
  }
  return res.json();
}

// Live updates (Server-Sent Events). Events carry only a small summary, so pages just
// re-fetch what they display. `onRefresh` is debounced: a burst of events causes ONE
// refresh. `onEach` (optional) sees every single event, e.g. to fill an activity feed.
function connectLive(onRefresh, { onEach, wait = 700 } = {}) {
  const refresh = debounce(onRefresh, wait);
  const es = new EventSource('/stream');
  es.onmessage = (e) => {
    let evt;
    try { evt = JSON.parse(e.data); } catch (err) { return; }
    if (onEach) onEach(evt);
    refresh();
  };
  return es;
}

// ----------------------------------------------------------------- location
// GPS only. An IP-based "approximate" location can be a whole city away, which is useless
// for pinpointing a pothole or for deciding which jobs are within a worker's reach.
function getCurrentLocation() {
  return new Promise((resolve, reject) => {
    if (!('geolocation' in navigator)) return reject(new Error('This device has no GPS support.'));
    if (window.isSecureContext === false) {
      return reject(new Error('GPS only works on localhost or HTTPS — open the app via http://localhost:5000.'));
    }
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy }),
      (err) => reject(new Error({
        1: 'Location permission was denied.', 2: 'Your position is unavailable.', 3: 'Locating timed out.',
      }[err.code] || 'GPS failed.')),
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
    );
  });
}

// Place search via OpenStreetMap Nominatim (free, no key).
async function searchPlaces(query, limit = 5) {
  const url = 'https://nominatim.openstreetmap.org/search?format=jsonv2&limit=' + limit +
              '&q=' + encodeURIComponent(query);
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error('Place search failed');
  return (await res.json()).map(r => ({
    name: r.display_name, lat: parseFloat(r.lat), lng: parseFloat(r.lon),
    bbox: r.boundingbox ? r.boundingbox.map(parseFloat) : null,   // [south, north, west, east]
  }));
}

// Phone cameras produce 3-10 MB photos. Shrinking in the browser keeps uploads fast and
// well under the server limit (the server re-encodes again anyway). Falls back to the
// original file if the browser can't decode it.
async function shrinkPhoto(file, maxSide = 1600) {
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
    const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', 0.85));
    return blob || file;
  } catch (e) {
    return file;
  }
}

// One reusable photo picker used by the report form and the resolve form.
// `capture` asks phones to open the camera directly; desktops just show a file dialog.
function setupPhotoPicker({ input, button, preview, previewImg, removeButton }) {
  let blob = null;
  const el = (id) => document.getElementById(id);
  button = el(button); input = el(input); preview = el(preview);
  previewImg = el(previewImg); removeButton = el(removeButton);

  button.addEventListener('click', () => input.click());
  input.addEventListener('change', async () => {
    const file = input.files[0];
    if (!file) return;
    if (!file.type.startsWith('image/')) { showToast('Please choose an image file.'); input.value = ''; return; }
    blob = await shrinkPhoto(file);
    previewImg.src = URL.createObjectURL(blob);
    preview.style.display = 'flex';
    button.style.display = 'none';
  });
  const clear = () => {
    blob = null; input.value = '';
    preview.style.display = 'none';
    button.style.display = '';
  };
  removeButton.addEventListener('click', clear);
  return { get: () => blob, clear };
}
