/* Shared browser logic for the Labelito web UI.
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

function authHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  const token = (localStorage.getItem(TOKEN_KEY) || '').trim();
  if (token) headers['Authorization'] = 'Bearer ' + token;
  return headers;
}

// Wire the page's #api-token input to localStorage. Call from the page script once the
// input exists. No-op when the page has no token input.
function initTokenInput() {
  const tokenInput = document.getElementById('api-token');
  if (!tokenInput) return null;
  tokenInput.value = localStorage.getItem(TOKEN_KEY) || '';
  tokenInput.addEventListener('input', () =>
    localStorage.setItem(TOKEN_KEY, tokenInput.value.trim()),
  );
  return tokenInput;
}

function handleAuthError(res) {
  if (res.status === 401) {
    showStatus('Authentication required — enter your API token below.', 'err');
    const tokenInput = document.getElementById('api-token');
    if (tokenInput) tokenInput.focus();
    return true;
  }
  return false;
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
const STATUS_POLL_MS = 4000; // base cadence while the printer is reachable
const STATUS_POLL_UNREACHABLE_MS = 15000; // slower cadence once a poll comes back unreachable
const STATUS_FETCH_TIMEOUT_MS = 8000; // abort a status fetch that hangs, so it can't wedge polling

function createStatusPoller({ tick, isHealthy }) {
  let timer = null;
  function schedule(ms) {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(run, ms);
  }
  async function run() {
    if (document.hidden) {
      schedule(STATUS_POLL_MS);
      return;
    }
    await tick();
    schedule(isHealthy() ? STATUS_POLL_MS : STATUS_POLL_UNREACHABLE_MS);
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

/* ── Init ─────────────────────────────────────────────────────────────────────
 * The shared head loads this script before <body> exists, so nav wiring waits for the
 * DOM. Pages' own inline scripts (end of body) run before this fires; anything they
 * need immediately (initTokenInput, pollers) they call directly. initLanguage is NOT
 * here — the nav partial calls it inline so it precedes the pages' initial preview.
 */
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
});
