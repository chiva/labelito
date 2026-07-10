/* Shared browser logic for the labelito web UI.
 *
 * Loaded as a classic script from the shared head partial, so everything here is a
 * deliberate global: the pages' inline scripts AND the Playwright e2e suite (via
 * page.evaluate) call these by name — keep signatures stable.
 *
 * XSS discipline (non-negotiable, e2e-enforced): every render path builds DOM nodes
 * and assigns textContent — never innerHTML. Messages routinely embed device/network
 * supplied text (SNMP media names, printer console strings, /print 409 details); on a
 * token-bearing page interpolating those into markup would let a hostile printer
 * string exfiltrate the API token from localStorage.
 */

/* ── URL prefixing ─────────────────────────────────────────────────────────────
 * All fetches/links go through api() so a reverse-proxy prefix (e.g. Home Assistant
 * ingress) only has to set window.LABELITO_BASE from server context. Default: none.
 */
function api(path) {
  return (window.LABELITO_BASE || '') + path;
}

/* ── Auth ────────────────────────────────────────────────────────────────────── */

const TOKEN_KEY = 'labelito_api_token';

// Whether a non-empty bearer token is stored. The single source of truth for the two token-attention
// states (needs-token / auth-failed) so their presence check can't drift.
function hasStoredToken() {
  return !!(localStorage.getItem(TOKEN_KEY) || '').trim();
}

function authHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  // Under HTTP Basic auth the browser attaches its own credential; injecting a bearer header here
  // would override it, so a stale token from a prior bearer deployment would 401 every request.
  if (window.LABELITO_BASIC_AUTH) return headers;
  const token = (localStorage.getItem(TOKEN_KEY) || '').trim();
  if (token) headers['Authorization'] = 'Bearer ' + token;
  return headers;
}

// Highlight the nav key button while no token is stored, so a first-run visitor on a secured
// deployment can find the entry before hitting a 401. No-op when the button is absent (Basic-auth
// and unauthenticated modes render no token UI at all).
function syncTokenIndicator() {
  const btn = document.getElementById('token-open');
  if (!btn) return;
  btn.classList.toggle('needs-token', !hasStoredToken());
}

// Wire the shared #api-token dialog input to localStorage. Call from the page script once the DOM
// exists. Returns the input, or null when the page renders no token UI (Basic/unauthenticated).
function initTokenInput() {
  const tokenInput = document.getElementById('api-token');
  if (!tokenInput) return null;
  tokenInput.value = localStorage.getItem(TOKEN_KEY) || '';
  syncTokenIndicator();
  tokenInput.addEventListener('input', () => {
    localStorage.setItem(TOKEN_KEY, tokenInput.value.trim());
    syncTokenIndicator();
    // Editing the token means the user is acting on the prior 401 — stop the wrong-token blink.
    document.getElementById('token-open')?.classList.remove('auth-failed');
  });
  return tokenInput;
}

function handleAuthError(res) {
  if (res.status !== 401) return false;
  if (window.LABELITO_BASIC_AUTH) {
    // Basic mode has no token UI — the browser holds the credential. A 401 here means the login
    // was dismissed or the cached credential is stale; a reload re-triggers the native prompt.
    showStatus('Authentication required — reload the page to sign in again.', 'err');
    return true;
  }
  // Bearer mode: point the user at the nav key button rather than force-opening the dialog. A 401
  // can come from a background fetch (e.g. the Print page's on-load /printer/status), and a native
  // modal there would blanket the page. The amber "needs-token" dot + this toast are the
  // non-blocking prompt; the user opens the dialog when ready.
  showStatus('Authentication required — enter your API token from the key icon.', 'err');
  syncTokenIndicator();
  // A token may be stored but WRONG, so needs-token (no-token only) won't fire. In that case blink the
  // key button red to point the user at where to fix it (cleared once they edit the input — see
  // initTokenInput). A tokenless 401 is the first-run case: leave the amber needs-token breathe alone,
  // since .auth-failed would otherwise override it (app.css) and mislabel "not set yet" as "rejected".
  const btn = document.getElementById('token-open');
  if (btn) btn.classList.toggle('auth-failed', hasStoredToken());
  return true;
}

/* ── Toasts (formerly the status banner) ──────────────────────────────────────
 * Same contract as before — showStatus(msg, type, {sticky}) / clearStatus({force}),
 * container #status-area, entry classes `status <type>` — restyled as a bottom-center
 * toast. One message at a time (replaceChildren), as the e2e assertions expect.
 * ok/info auto-dismiss quickly; errors and sticky successes stay longer and carry a
 * dismiss button so device-supplied detail can actually be read.
 */
let statusTimer = null;
const STATUS_TOAST_MS = 3600;
const STATUS_STICKY_MS = 8000;

const TOAST_ICONS = { ok: 'circle-check', err: 'triangle-exclamation', info: 'circle-info' };

// Clone an inline-SVG icon from the hidden <template id="icon-defs"> the nav partial
// emits. Returns null when the template (or icon) is missing so callers can degrade.
function iconNode(name) {
  const defs = document.getElementById('icon-defs');
  if (!defs) return null;
  const svg = defs.content.querySelector(`svg[data-icon="${name}"]`);
  return svg ? svg.cloneNode(true) : null;
}

function showStatus(msg, type, { sticky } = {}) {
  const area = document.getElementById('status-area');
  if (!area) return;
  if (statusTimer !== null) {
    clearTimeout(statusTimer);
    statusTimer = null;
  }
  const div = document.createElement('div');
  div.className = `status toast ${type}`;
  const icon = iconNode(TOAST_ICONS[type] || 'circle-info');
  if (icon) div.appendChild(icon);
  // Message is always a text node — see the XSS note in the file header.
  div.appendChild(document.createTextNode(msg));
  const dismissable = sticky || type === 'err';
  if (dismissable) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'toast-close close';
    btn.setAttribute('aria-label', 'Dismiss');
    btn.textContent = '×';
    btn.addEventListener('click', () => clearStatus({ force: true }));
    div.appendChild(btn);
  }
  area.replaceChildren(div);
  if (sticky) {
    area.dataset.sticky = '1';
    statusTimer = setTimeout(() => clearStatus({ force: true }), STATUS_STICKY_MS);
  } else {
    delete area.dataset.sticky;
    statusTimer = setTimeout(
      () => clearStatus({ force: true }),
      dismissable ? STATUS_STICKY_MS : STATUS_TOAST_MS,
    );
  }
}

function clearStatus({ force } = {}) {
  const area = document.getElementById('status-area');
  if (!area) return;
  if (area.dataset.sticky && !force) return;
  area.replaceChildren();
  delete area.dataset.sticky;
  if (statusTimer !== null) {
    clearTimeout(statusTimer);
    statusTimer = null;
  }
}

// Uniform "<status> · <detail>" error text. `detail` may be a string (FastAPI's usual
// shape) or structured (422 validation lists) — non-strings are JSON.stringify'd. The
// result is always rendered via textContent, so stringified content stays inert.
function formatError(status, detail) {
  const text = typeof detail === 'string' ? detail : JSON.stringify(detail ?? 'error');
  return status ? `${status} · ${text}` : text;
}

/* ── Small utilities ─────────────────────────────────────────────────────────── */

function debounce(fn, ms) {
  let t;
  return function (...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), ms);
  };
}

/* ── Media compatibility helpers ──────────────────────────────────────────────
 * Client-side mirror of the server's app.media.media_matches — tolerances and the
 * width/form/length rule must match the server exactly.
 */
const WIDTH_TOLERANCE_MM = 1.0;
const LENGTH_TOLERANCE_MM = 1.0;

// The loaded roll from a /printer/status body as {width_mm, media_type, length_mm},
// or null when it can't be compared: unreachable, a transport with no readable media
// (file://), or a non-canonical media_type → unknown.
function loadedMediaFrom(status) {
  if (!status || !status.reachable) return null;
  const uri = status.uri;
  const queryable =
    typeof uri === 'string' && (uri.startsWith('tcp://') || uri.startsWith('usb://'));
  if (!queryable) return null;
  if (status.media_width_mm == null) return null;
  if (status.media_type !== 'continuous' && status.media_type !== 'die_cut') return null;
  return {
    width_mm: status.media_width_mm,
    media_type: status.media_type,
    length_mm: status.media_length_mm,
  };
}

function mediaDesc(m) {
  if (!m || m.width_mm == null || !m.media_type) return 'unknown media';
  const kind = m.media_type === 'die_cut' ? 'die-cut' : m.media_type;
  if (m.media_type === 'die_cut' && m.length_mm != null) {
    return `${m.width_mm}mm×${m.length_mm}mm ${kind}`;
  }
  return `${m.width_mm}mm ${kind}`;
}

// 'match' | 'mismatch' | 'unknown' — mirrors app.media.media_matches.
function mediaCompat(required, loaded) {
  if (!required || !loaded) return 'unknown';
  if (Math.abs(required.width_mm - loaded.width_mm) > WIDTH_TOLERANCE_MM) return 'mismatch';
  if (required.media_type !== loaded.media_type) return 'mismatch';
  if (
    required.media_type === 'die_cut' &&
    required.length_mm != null &&
    loaded.length_mm != null &&
    Math.abs(required.length_mm - loaded.length_mm) > LENGTH_TOLERANCE_MM
  ) {
    return 'mismatch';
  }
  return 'match';
}

// Stable group key for a media object (null → 'other'). Buckets templates and detects
// roll swaps between polls.
function groupKeyOf(media) {
  if (!media || media.width_mm == null) return 'other';
  if (media.media_type === 'die_cut') return `d:${media.width_mm}x${media.length_mm}`;
  return `c:${media.width_mm}`;
}

function groupTitleOf(media, fallbackLabel) {
  if (!media || media.width_mm == null) {
    return fallbackLabel ? `Other — ${fallbackLabel}` : 'Other / unknown size';
  }
  if (media.media_type === 'die_cut') return `${media.width_mm}×${media.length_mm}mm die-cut`;
  return `${media.width_mm}mm continuous`;
}

// Sort order: continuous (by width), then die-cut (by width, then length), then 'other'.
function groupSortKey(media) {
  if (!media || media.width_mm == null) return [2, 0, 0];
  if (media.media_type === 'die_cut') return [1, media.width_mm, media.length_mm || 0];
  return [0, media.width_mm, 0];
}

/* ── Status polling ───────────────────────────────────────────────────────────
 * Shared self-scheduling poll: a setTimeout chain (not setInterval) so the cadence
 * varies with the last result and never stacks ticks. Skips work while the tab is
 * hidden; backs off when the printer is unreachable. Pages provide the tick (their
 * refresh function) and a health check for cadence selection.
 */
const STATUS_POLL_MS = 4000; // base cadence while the printer is reachable and idle
const STATUS_POLL_BUSY_MS = 1000; // fast cadence while a job is in flight, so the badge converges
// back to Idle within ~1s of the printer reporting it instead of on the next 4s boundary
const STATUS_POLL_UNREACHABLE_MS = 15000; // slower cadence once a poll comes back unreachable
const STATUS_FETCH_TIMEOUT_MS = 8000; // abort a status fetch that hangs, so it can't wedge polling

// Pages pass tick (their refresh fn) and isHealthy (cadence: reachable→base, else back off). An
// optional isBusy predicate opts into the fast cadence while it returns true (a print in flight or the
// printer still reporting a working state), so the poll tightens during a print and relaxes when idle.
function createStatusPoller({ tick, isHealthy, isBusy }) {
  let timer = null;
  function schedule(ms) {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(run, ms);
  }
  function nextDelay() {
    if (!isHealthy()) return STATUS_POLL_UNREACHABLE_MS; // can't poll a dead host fast; back off
    if (isBusy && isBusy()) return STATUS_POLL_BUSY_MS;
    return STATUS_POLL_MS;
  }
  async function run() {
    if (document.hidden) {
      schedule(STATUS_POLL_MS);
      return;
    }
    await tick();
    schedule(nextDelay());
  }
  // Resume immediately when the tab regains focus (polls paused while hidden go stale fast).
  document.addEventListener('visibilitychange', () => {
    // Route through schedule() so the pending timer is cleared first — a direct run() could race
    // an about-to-fire timeout and put two tick() calls in flight at once.
    if (timer !== null && !document.hidden) schedule(0);
  });
  return {
    start() {
      schedule(STATUS_POLL_MS);
    },
    schedule,
  };
}

// fetch() with an abort timeout, used by the pages' status refreshes so a hung fetch
// can't pin their in-flight guard forever.
async function abortableFetch(url, opts = {}, timeoutMs = STATUS_FETCH_TIMEOUT_MS) {
  const controller = new AbortController();
  const abortTimer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...opts, signal: controller.signal });
  } finally {
    clearTimeout(abortTimer);
  }
}

/* ── Theme ────────────────────────────────────────────────────────────────────
 * The FOUC-free initial theme is set by an inline script in the shared head partial;
 * this only wires the nav toggle. An explicit toggle persists the choice.
 */
const THEME_KEY = 'labelito_theme';

function initTheme() {
  const toggle = document.getElementById('theme-toggle');
  const media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
  let saved = null;
  try {
    saved = localStorage.getItem(THEME_KEY);
  } catch (e) {
    /* storage disabled — keep following the OS scheme for this page view */
  }
  // Live-follow the OS scheme (light/dark) until the user makes an explicit choice — the head
  // partial's inline script already resolves the SAME priority (saved > OS preference > dark) once,
  // before first paint, to avoid a flash; this only ADDS live tracking for as long as no explicit
  // choice exists. Detection only ever READS matchMedia — it must never itself write to localStorage,
  // or a browser-driven default would masquerade as a real user choice and stop tracking future OS
  // changes (mirrors the browser-language default in initLanguage below).
  let onSchemeChange = null;
  if (media && saved !== 'light' && saved !== 'dark') {
    onSchemeChange = (e) => {
      document.documentElement.dataset.theme = e.matches ? 'light' : 'dark';
    };
    media.addEventListener('change', onSchemeChange);
  }
  if (!toggle) return;
  toggle.addEventListener('click', () => {
    // An explicit choice wins from here on — stop following the OS scheme.
    if (onSchemeChange && media) {
      media.removeEventListener('change', onSchemeChange);
      onSchemeChange = null;
    }
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (e) {
      /* storage disabled — theme still flips for this page view */
    }
  });
}

/* ── Language ─────────────────────────────────────────────────────────────────
 * The nav <select id="language-select"> is server-rendered with the available label
 * languages, defaulting to the server's DEFAULT_LANGUAGE. A persisted choice wins.
 * currentLanguage() feeds the preview/print payloads' `language` field.
 */
const LANGUAGE_KEY = 'labelito_language';
const languageChangeHandlers = [];

function currentLanguage() {
  const sel = document.getElementById('language-select');
  return sel && sel.value ? sel.value : null;
}

function onLanguageChange(fn) {
  languageChangeHandlers.push(fn);
}

/* Called inline by the nav partial right after the select is parsed — NOT at DOMContentLoaded —
 * so the resolved language is already live when the pages' end-of-body scripts fire their
 * initial preview (a later restore would preview one language and print another). */
function initLanguage() {
  const sel = document.getElementById('language-select');
  if (!sel) return;
  let saved = null;
  try {
    saved = localStorage.getItem(LANGUAGE_KEY);
  } catch (e) {
    /* storage disabled — fall through to browser-language detection */
  }
  if (saved && Array.from(sel.options).some((o) => o.value === saved)) {
    sel.value = saved;
  } else {
    // No explicit user override: default to the browser's language when one of its subtags matches
    // an available option. This ONLY reads navigator.language(s) — it must never itself write to
    // localStorage, so the default keeps tracking the browser's language (e.g. across a locale
    // change) until the user actually picks one via the change listener below, which is the sole
    // place a choice is persisted.
    const browserLangs = navigator.languages && navigator.languages.length
      ? navigator.languages
      : navigator.language
        ? [navigator.language]
        : [];
    const available = Array.from(sel.options).map((o) => o.value);
    for (const lang of browserLangs) {
      const primary = lang.split('-')[0].toLowerCase();
      const match = available.find((v) => v.toLowerCase() === primary);
      if (match) {
        sel.value = match;
        break;
      }
    }
  }
  sel.addEventListener('change', () => {
    try {
      localStorage.setItem(LANGUAGE_KEY, sel.value);
    } catch (e) {
      /* storage disabled — the choice still applies for this page view */
    }
    for (const fn of languageChangeHandlers) fn(sel.value);
  });
}

/* ── Image fields ───────────────────────────────────────────────────────────────
 * A template field backed by an `image` layout element (server-reported in a
 * template's `image_fields`) is filled by choosing/dropping an image file, not by
 * typing. The browser reads the file, base64-encodes it, and the page's payload
 * builder merges that base64 into `fields[<name>]` — the SAME shape the JSON /print,
 * /preview and /preview/draft routes already accept (see app/main.py). We deliberately
 * do NOT use /preview/multipart (it hardcodes the "image" field name and is
 * preview-only); one base64 representation covers both preview and print.
 *
 * XSS discipline (see the file header): the widget is built entirely from DOM nodes and
 * textContent — never innerHTML. The thumbnail src is a data: URL derived from the
 * user's own local file, never device/network text.
 */

// Mirror app.main.MAX_IMAGE_UPLOAD_BYTES (5 MiB decoded). Reject oversized files client-side with a
// friendly toast rather than letting the server 413 after a wasted upload.
const IMAGE_MAX_UPLOAD_BYTES = 5 * 1024 * 1024;

// fieldName → {dataUrl, filename}. The full FileReader data URL is kept (not just the base64
// payload) so a widget rebuilt in place — the Studio re-renders the field form on every keystroke —
// can redraw its thumbnail from cache without re-reading the file. Only one page is live at a time,
// so a module-level map is sufficient; reset on template load / template switch.
const imageFieldData = new Map();

// fieldName → read-generation. FileReader is async, so a slow read for a large file can finish AFTER
// the user re-picked, cleared, or switched templates. Each read captures the current generation;
// its onload only commits if the field's generation is still that value. Picking (a new read),
// clearing, and resetting all bump the generation, so a superseded read is dropped instead of
// clobbering the current value (which would otherwise ride into /preview and /print).
const imageFieldGen = new Map();
let _imageReadSeq = 0;

// In-flight FileReader reads. While any is pending the print page disables its Print button (see
// syncImageReadState), so a print is only ever built once the chosen image is committed to the
// cache — never from a half-loaded state that would submit the label WITHOUT the just-chosen image
// (the server accepts an absent optional image and prints blank). Keeping the print snapshot fully
// synchronous (no await) also means fields/template can't change between click and send.
const _pendingImageReads = new Set();

// Whether any image read is still in flight.
function imageReadsPending() {
  return _pendingImageReads.size > 0;
}

// Announce a change in pending-read state so a page can reflect it (e.g. toggle the Print button).
// Guarded for the (test/headless) case where document is momentarily unavailable.
function _notifyImageReadState() {
  if (typeof document !== 'undefined' && document.dispatchEvent) {
    document.dispatchEvent(new CustomEvent('labelito:image-reads-changed'));
  }
}

// Strip a FileReader data URL ("data:image/png;base64,AAAA…") down to the base64 payload the render
// element decodes. Returns "" for an unexpected shape so a broken read can't smuggle a data: prefix
// into the field (the server would then fail to decode it).
function _dataUrlToBase64(dataUrl) {
  const comma = typeof dataUrl === 'string' ? dataUrl.indexOf(',') : -1;
  return comma >= 0 ? dataUrl.slice(comma + 1) : '';
}

// Read + validate a chosen/dropped File into imageFieldData[name], then refresh the widget preview
// and notify the caller. Rejects a non-image type or an over-cap file with a toast; a rejected pick
// leaves any already-COMMITTED image intact but still supersedes an older in-flight read.
//
// `onSelect` fires SYNCHRONOUSLY here on any real selection/drop, before the async FileReader — the
// print page uses it to mark the form user-edited immediately, so a background /printer/status poll
// that lands during the read can't refocus the picker and discard the just-chosen image.
function _acceptImageFile(name, file, wrap, onChange, onSelect) {
  if (!file) return;
  if (onSelect) onSelect();
  // Claim a new generation on EVERY real selection/drop, BEFORE validation: picking (even an invalid)
  // file supersedes an older read still in flight, so that read is discarded instead of committing a
  // stale image that Print would then wait for and submit. A rejected pick does not touch
  // already-committed data below, so a prior valid image is preserved.
  const gen = ++_imageReadSeq;
  imageFieldGen.set(name, gen);
  if (file.type && !file.type.startsWith('image/')) {
    showStatus(`"${file.name}" is not an image file.`, 'err');
    return;
  }
  if (file.size > IMAGE_MAX_UPLOAD_BYTES) {
    const mb = (IMAGE_MAX_UPLOAD_BYTES / (1024 * 1024)).toFixed(0);
    showStatus(`"${file.name}" is too large (max ${mb} MB).`, 'err');
    return;
  }
  const reader = new FileReader();
  // Tracked in _pendingImageReads so the page can disable Print until this read commits.
  const done = new Promise((resolve) => {
    reader.onload = () => {
      try {
        if (imageFieldGen.get(name) !== gen) return; // superseded by a newer pick / clear / reset
        const b64 = _dataUrlToBase64(reader.result);
        if (!b64) {
          showStatus(`Could not read "${file.name}".`, 'err');
          return;
        }
        imageFieldData.set(name, { dataUrl: reader.result, filename: file.name });
        _renderImagePreview(wrap, name, file.name, reader.result);
        if (onChange) onChange();
      } finally {
        resolve();
      }
    };
    reader.onerror = () => {
      try {
        if (imageFieldGen.get(name) === gen) showStatus(`Could not read "${file.name}".`, 'err');
      } finally {
        resolve();
      }
    };
  });
  _pendingImageReads.add(done);
  _notifyImageReadState();
  done.finally(() => {
    _pendingImageReads.delete(done);
    _notifyImageReadState();
  });
  reader.readAsDataURL(file);
}

// (Re)draw the chosen-file state of a widget: a thumbnail, the file name, and a clear button; or the
// empty dropzone prompt when no file is held. DOM/textContent only.
function _renderImagePreview(wrap, name, fileName, dataUrl) {
  const zone = wrap.querySelector('.image-dropzone');
  if (!zone) return;
  zone.replaceChildren();
  if (dataUrl) {
    zone.classList.add('has-image');
    const thumb = document.createElement('img');
    thumb.className = 'image-thumb';
    thumb.alt = fileName || name;
    thumb.src = dataUrl; // a local data: URL from the user's own file — safe, not device text
    const label = document.createElement('span');
    label.className = 'image-filename';
    label.textContent = fileName || 'image';
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'image-clear close';
    clear.setAttribute('aria-label', 'Remove image');
    clear.textContent = '×';
    clear.addEventListener('click', (e) => {
      e.stopPropagation(); // don't re-open the file dialog the dropzone click handler would trigger
      clearImageField(name);
      const input = wrap.querySelector('input[type=file]');
      if (input) input.value = ''; // let the same file be re-picked after a clear
      _renderImagePreview(wrap, name, null, null);
      wrap.dispatchEvent(new CustomEvent('image-cleared', { bubbles: false }));
    });
    zone.append(thumb, label, clear);
  } else {
    zone.classList.remove('has-image');
    const prompt = document.createElement('span');
    prompt.className = 'image-prompt';
    prompt.textContent = 'Click or drop an image';
    zone.appendChild(prompt);
  }
}

// Build a file-picker + drag-drop widget for an image field. `onChange` fires after a file is
// accepted OR cleared (the page uses it to re-preview). `onSelect` (optional) fires SYNCHRONOUSLY
// the instant a file is selected/dropped/cleared — before the async read — so the page can mark the
// form user-edited before any background poll can interleave. The hidden <input> keeps the shared
// id="field-<name>" convention so existing focus/e2e selectors keep working.
function buildImageField(name, onChange, onSelect) {
  const wrap = document.createElement('div');
  wrap.className = 'image-field';

  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.id = `field-${name}`;
  input.name = name;
  input.className = 'image-file-input';
  input.hidden = true;
  input.addEventListener('change', () =>
    _acceptImageFile(name, input.files && input.files[0], wrap, onChange, onSelect),
  );

  const zone = document.createElement('div');
  zone.className = 'image-dropzone';
  zone.tabIndex = 0;
  zone.setAttribute('role', 'button');
  zone.setAttribute('aria-label', `Choose an image for ${name}`);
  const openPicker = () => input.click();
  zone.addEventListener('click', openPicker);
  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openPicker();
    }
  });
  zone.addEventListener('dragover', (e) => {
    e.preventDefault();
    zone.classList.add('dragover');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    zone.classList.remove('dragover');
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    _acceptImageFile(name, file, wrap, onChange, onSelect);
  });

  // Clearing (the × button, dispatched from _renderImagePreview) marks the form user-edited (onSelect)
  // and re-previews (onChange) so the label reflects the now-removed image — same paths as an accept.
  wrap.addEventListener('image-cleared', () => {
    if (onSelect) onSelect();
    if (onChange) onChange();
  });

  wrap.append(input, zone);
  // Redraw from cache when rebuilt in place (Studio re-parse) so the thumbnail survives; otherwise
  // show the empty prompt.
  const cached = imageFieldData.get(name);
  _renderImagePreview(wrap, name, cached ? cached.filename : null, cached ? cached.dataUrl : null);
  return wrap;
}

// Cached base64 payload (no data: prefix) for one image field, or null when none is held.
function imageFieldValue(name) {
  const entry = imageFieldData.get(name);
  return entry ? _dataUrlToBase64(entry.dataUrl) : null;
}

// {name: base64} for the given field names that currently hold an image — spread into a payload's
// `fields` by the page builders.
function collectImageFields(names) {
  const out = {};
  for (const name of names || []) {
    const b64 = imageFieldValue(name);
    if (b64) out[name] = b64;
  }
  return out;
}

// Drop one field's cached image (the widget redraw is the caller's concern). Bumps the field's read
// generation so an in-flight read for it can't repopulate the cache after the clear.
function clearImageField(name) {
  imageFieldData.delete(name);
  imageFieldGen.set(name, ++_imageReadSeq);
}

// Drop every cached image — call when switching templates / re-detecting fields so one template's
// image never leaks into another's form. Clearing the generation map also invalidates any in-flight
// read (its captured generation no longer matches the now-absent entry).
function resetImageFields() {
  imageFieldData.clear();
  imageFieldGen.clear();
}

/* ── About modal ────────────────────────────────────────────────────────────────
 * Static rows are server-rendered (see partials/_about.html); the runtime rows are filled
 * once from GET /health the first time the modal opens. /health is unauthenticated, so no
 * token header is needed. Failure degrades to "unavailable" rather than blocking the modal,
 * and only a success latches so a transient failure retries on the next open.
 */
let aboutRuntimeLoaded = false;
async function loadAboutRuntime() {
  if (aboutRuntimeLoaded) return;
  const set = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value; // /health is app-config, but keep textContent for consistency
    el.classList.remove('muted');
  };
  try {
    const res = await fetch(api('/health'), {headers: {Accept: 'application/json'}});
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    set('about-model', data.model || 'unknown');
    set('about-transport', data.transport || 'unknown');
    set('about-templates', data.template_count != null ? String(data.template_count) : 'unknown');
    aboutRuntimeLoaded = true;
  } catch (e) {
    for (const id of ['about-model', 'about-transport', 'about-templates']) {
      const el = document.getElementById(id);
      if (el) el.textContent = 'unavailable';
    }
  }
}

// Close a native <dialog> only on a true backdrop click. A click whose target IS the dialog element
// can be either the ::backdrop OR the dialog's own padding/margins (e.g. the gaps between the About
// modal's sections) — both report e.target === dlg. Only the backdrop lies OUTSIDE the dialog's
// bounding rect, so gate on that; clicking blank space inside the modal must not close it.
function closeDialogOnBackdropClick(dlg) {
  dlg.addEventListener('click', (e) => {
    if (e.target !== dlg) return; // a content element was clicked, not the dialog box itself
    const r = dlg.getBoundingClientRect();
    const outside =
      e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    if (outside) dlg.close();
  });
}

function initAbout() {
  const dlg = document.getElementById('about-dialog');
  const openBtn = document.getElementById('about-open');
  const closeBtn = document.getElementById('about-close');
  if (!dlg || !openBtn) return;
  openBtn.addEventListener('click', () => {
    dlg.showModal(); // native modal → Esc-to-close + backdrop come for free
    loadAboutRuntime();
  });
  if (closeBtn) closeBtn.addEventListener('click', () => dlg.close());
  closeDialogOnBackdropClick(dlg);
}

/* ── Update check ───────────────────────────────────────────────────────────────
 * Asks the server (GET /update-check, public like /health — no token) whether a newer release
 * exists. When one does, flags the nav info button with a static dot (.has-update) and fills the
 * About modal's #about-update note with a link to the release. Fails soft: any error, a disabled
 * check, or an up-to-date result simply leaves the nav untouched. The server caches the GitHub
 * lookup, so calling this on every page load is cheap.
 */
async function initUpdateCheck() {
  const openBtn = document.getElementById('about-open');
  if (!openBtn) return;
  let data;
  try {
    const res = await fetch(api('/update-check'), {headers: {Accept: 'application/json'}});
    if (!res.ok) return;
    data = await res.json();
  } catch (e) {
    return; // network/parse failure — never block the nav on a version check
  }
  if (!data.enabled || data.latest == null) return;
  const note = document.getElementById('about-update');
  if (data.update_available) {
    openBtn.classList.add('has-update');
    openBtn.title = `About labelito — update available (v${data.latest})`;
    if (note && data.release_url) {
      const link = document.createElement('a');
      link.href = data.release_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = `v${data.latest} available`;
      note.replaceChildren(link);
    }
  } else if (note) {
    note.textContent = 'up to date';
    note.classList.add('muted');
  }
}

/* ── Copy config ────────────────────────────────────────────────────────────────
 * The About modal's "Copy config" button copies a plain-text, credential-free config snapshot
 * (GET /diagnostics — public, redacted server-side) so a user can paste it straight into a GitHub
 * issue. buildDiagnosticsText is a pure formatter; copyText handles the plain-HTTP fallback.
 */
function buildDiagnosticsText(d) {
  const bool = (v) => (v ? 'yes' : 'no');
  const rows = [
    ['Version', `${d.version} (API v${d.api_version})`],
    ['Model', d.model],
    ['Driver', d.driver],
    ['Printer URI', d.printer_uri],
    ['Transport', d.transport],
    ['SNMP', d.snmp_enabled ? `enabled (port ${d.snmp_port})` : 'disabled'],
    ['Auth mode', d.auth_mode],
    ['History', `${d.history_mode} (browse UI: ${bool(d.history_ui)})`],
    ['Editor', bool(d.editor_enabled)],
    ['Templates writable', bool(d.templates_writable)],
    ['Templates loadable', bool(d.templates_loadable)],
    ['Templates loaded', `${d.template_count} (examples: ${bool(d.load_examples)})`],
    ['Language', `${d.default_language} (available: ${d.languages.join(', ')})`],
    ['Metrics', bool(d.metrics_enabled)],
    ['Proxy path header', d.proxy_path_header || '(unset)'],
    [
      'Render defaults',
      `dither=${bool(d.default_dither)} threshold=${d.default_threshold} ` +
        `high_res=${bool(d.default_high_res)} red=${bool(d.default_red)}`,
    ],
    ['Length bounds', `${d.min_length_px}–${d.max_length_px} px`],
    ['Python', d.python_version],
    ['Platform', d.platform],
  ];
  const width = Math.max(...rows.map(([k]) => k.length));
  const body = rows.map(([k, v]) => `${(k + ':').padEnd(width + 1)} ${v}`).join('\n');
  return `labelito diagnostics\n====================\n${body}\n`;
}

// Copy text to the clipboard, falling back to a hidden-textarea + execCommand for plain-HTTP
// deployments (navigator.clipboard is undefined outside a secure context). Resolves true on success.
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      /* fall through to the legacy path */
    }
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

function initCopyConfig() {
  const btn = document.getElementById('about-copy-config');
  if (!btn) return;
  const original = btn.textContent;
  let resetTimer = null;
  const flash = (msg) => {
    btn.textContent = msg;
    if (resetTimer !== null) clearTimeout(resetTimer);
    // Inline feedback on the button — a #status-area toast would render BEHIND the modal's top layer.
    resetTimer = setTimeout(() => {
      btn.textContent = original;
      resetTimer = null;
    }, 1500);
  };
  btn.addEventListener('click', async () => {
    // /diagnostics is credential-gated (like GET /templates) — send authHeaders() so bearer callers
    // attach their token; under Basic auth the browser attaches its own credential automatically.
    let data;
    try {
      const res = await fetch(api('/diagnostics'), {headers: {...authHeaders(), Accept: 'application/json'}});
      if (res.status === 401) {
        // handleAuthError is mode-aware (bearer: light the nav key button; Basic: reload-to-sign-in).
        // Its #status-area toast renders behind the modal's top layer, so the inline flash is the cue
        // the user actually sees over the modal — kept mode-neutral to fit both auth modes.
        handleAuthError(res);
        flash('Sign-in required');
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      data = await res.json();
    } catch (e) {
      flash('Couldn’t copy — try again');
      return;
    }
    const ok = await copyText(buildDiagnosticsText(data));
    flash(ok ? 'Copied ✓' : 'Couldn’t copy — try again');
  });
}

// API-token dialog — same native-<dialog> pattern as initAbout. Present only in bearer mode (the
// nav renders the button + dialog under browser_token_entry); a no-op otherwise.
function initTokenDialog() {
  const dlg = document.getElementById('token-dialog');
  const openBtn = document.getElementById('token-open');
  const closeBtn = document.getElementById('token-close');
  if (!dlg || !openBtn) return;
  openBtn.addEventListener('click', () => dlg.showModal());
  if (closeBtn) closeBtn.addEventListener('click', () => dlg.close());
  closeDialogOnBackdropClick(dlg);
}

/* ── Init ─────────────────────────────────────────────────────────────────────
 * The shared head loads this script before <body> exists, so nav wiring waits for the
 * DOM. Pages' own inline scripts (end of body) run before this fires; anything they
 * need immediately (initTokenInput, pollers) they call directly. initLanguage is NOT
 * here — the nav partial calls it inline so it precedes the pages' initial preview.
 */
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initAbout();
  initUpdateCheck();
  initCopyConfig();
  initTokenDialog();
});
