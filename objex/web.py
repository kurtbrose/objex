import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .explorer import PathFailure, Reader


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>objex web</title>
  <link rel="stylesheet" href="/styles.css">
</head>
<body>
  <header class="topbar">
    <a class="brand" href="/">objex web</a>
    <form id="jump-form"><input id="jump-id" placeholder="Object ID"><button>Go</button></form>
    <form id="path-form"><input id="path-input" placeholder="Path like sys.modules"><button>Path</button></form>
    <form id="type-form"><input id="type-input" placeholder="Type search"><button>Type</button></form>
    <button id="random-btn" type="button">Random</button>
  </header>
  <section id="summary" class="summary"></section>
  <section id="loading" class="loading hidden"></section>
  <section id="message" class="message"></section>
  <main class="layout">
    <section id="inbound-panel" class="panel"></section>
    <aside id="object-panel" class="panel"></aside>
    <section id="outbound-panel" class="panel"></section>
  </main>
  <section id="paths-panel" class="panel"></section>
  <section id="discovery-panel" class="panel"></section>
  <section id="root-summary-panel" class="panel"></section>
  <section id="marks-panel" class="panel"></section>
  <section id="search-results" class="panel search-results"></section>
  <script src="/app.js"></script>
</body>
</html>
"""


APP_JS = """const state = { currentObjectId: null, loadingCount: 0 };

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>\\"]/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '\\"': '&quot;'}[ch]));
}

async function fetchJson(url) {
  setLoading(true);
  const response = await fetch(url);
  try {
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || `Request failed: ${response.status}`);
    }
    return payload;
  } finally {
    setLoading(false);
  }
}

function objectLink(obj) {
  return `<a class="object-link" href="/?id=${encodeURIComponent(obj.id)}" data-object-id="${obj.id}">${escapeHtml(obj.label)}</a>`;
}

function pathLink(path, label) {
  return `<a class="object-link path-link" href="/?path=${encodeURIComponent(path)}" data-go-path="${escapeHtml(path)}">${escapeHtml(label)}</a>`;
}

function renderModulePathLinks(moduleName) {
  const segments = moduleName.split('.');
  const parts = [];
  for (let i = 0; i < segments.length; i += 1) {
    const prefix = segments.slice(0, i + 1).join('.');
    parts.push(pathLink(prefix, segments[i]));
  }
  return parts.join('<span class="path-sep">.</span>');
}

function renderSummary(summary) {
  document.getElementById('summary').innerHTML = `
    <div><strong>${escapeHtml(summary.path)}</strong></div>
    <div>${escapeHtml(summary.hostname)} at ${escapeHtml(summary.timestamp)}</div>
    <div>${summary.object_count.toLocaleString()} objects, ${summary.reference_count.toLocaleString()} references</div>
    <div>${summary.memory_mb.toFixed(1)} MiB RSS, ${(summary.visible_memory_fraction * 100).toFixed(1)}% visible</div>
  `;
}

function setObjectMode(hasObject) {
  document.body.classList.toggle('object-mode', hasObject);
  document.body.classList.toggle('landing-mode', !hasObject);
}

function renderRootSummaryList(title, items) {
  const isModuleList = title.indexOf('Module') !== -1;
  return `
    <div>
      <h3>${title}</h3>
      <ul class="refs">
        ${(items || []).length ? items.map(item => `<li><span class="edge">${item.count}</span>${item.object ? objectLink(item.object) : `<span>${escapeHtml(item.label)}</span>`}${item.object ? `<span class="type">${isModuleList ? renderModulePathLinks(item.label) : escapeHtml(item.label)}</span>` : ''}</li>`).join('') : '<li class="empty">No entries</li>'}
      </ul>
    </div>
  `;
}

function renderDiscovery(topTypes, largestObjects) {
  document.getElementById('discovery-panel').innerHTML = `
    <h2>Discovery</h2>
    <div class="discovery-grid">
      <div>
        <h3>Top Types</h3>
        <ul class="refs">
          ${topTypes.items.map(item => `<li>${objectLink({id: item.type_id, label: `<type ${item.name}#${item.type_id}>`})} <span class="type">${item.instance_count.toLocaleString()} instances</span> <span class="edge">${item.memory_percent.toFixed(1)}%</span></li>`).join('')}
        </ul>
      </div>
      <div>
        <h3>Largest Objects</h3>
        <ul class="refs">
          ${largestObjects.items.map(item => `<li>${objectLink(item.object)} <span class="type">${escapeHtml(item.object.typequalname)}</span> <span class="edge">${item.size} bytes</span></li>`).join('')}
        </ul>
      </div>
    </div>
  `;
}

function renderRootSummaryLoading(sampleSize) {
  document.getElementById('root-summary-panel').innerHTML = `
    <h2>Sampled Root Summary</h2>
    <div class="loading-inline"><span class="spinner" aria-hidden="true"></span><span>Sampling ${sampleSize} random objects…</span></div>
  `;
}

function renderRootSummary(rootSummary) {
  document.getElementById('root-summary-panel').innerHTML = `
    <div class="panel-header">
      <h2>Sampled Root Summary</h2>
      <button id="reload-root-summary" type="button">Refresh</button>
    </div>
    <div class="subtle">Sampled ${rootSummary.sample_size} random objects</div>
    <div class="discovery-grid">
      ${renderRootSummaryList('Module Roots', rootSummary.module_roots)}
      ${renderRootSummaryList('Module Paths', rootSummary.module_paths)}
      ${renderRootSummaryList('Frame Roots', rootSummary.frame_roots)}
      ${renderRootSummaryList('Frame Paths', rootSummary.frame_paths)}
    </div>
  `;
}

function renderRootSummaryError(message) {
  document.getElementById('root-summary-panel').innerHTML = `
    <div class="panel-header">
      <h2>Sampled Root Summary</h2>
      <button id="reload-root-summary" type="button">Retry</button>
    </div>
    <div class="empty">${escapeHtml(message)}</div>
  `;
}

function renderObjectPanel(obj) {
  document.getElementById('object-panel').innerHTML = `
    <h2>Current Object</h2>
    <div class="object-label">${escapeHtml(obj.label)}</div>
    <form id="mark-form" class="mark-form">
      <input id="mark-input" placeholder="bookmark name">
      <button>Mark</button>
    </form>
    <div class="mark-list">${obj.marks.length ? obj.marks.map(mark => `<span class="mark-chip">${escapeHtml(mark)}</span>`).join('') : '<span class="empty">No marks</span>'}</div>
    <dl class="meta">
      <dt>ID</dt><dd>${obj.id}</dd>
      <dt>Type</dt><dd>${escapeHtml(obj.typequalname)}</dd>
      <dt>Size</dt><dd>${obj.size}</dd>
      <dt>Refcount</dt><dd>${escapeHtml(obj.refcount_display ?? String(obj.refcount))}</dd>
      <dt>Len</dt><dd>${obj.len ?? ''}</dd>
    </dl>
  `;
}

function renderMarksPanel(marksPayload) {
  const items = marksPayload.items || [];
  document.getElementById('marks-panel').innerHTML = `
    <h2>Bookmarks</h2>
    <ul class="refs">
      ${items.length ? items.map(item => `<li><span class="edge">${escapeHtml(item.mark)}</span> ${objectLink(item.object)}</li>`).join('') : '<li class="empty">No bookmarks yet</li>'}
    </ul>
  `;
}

function renderRefs(elementId, title, data) {
  const items = data.items.map(item => `
    <li>
      <span class="edge">${escapeHtml(item.ref)}</span>
      ${objectLink(item.object)}
      <span class="type">${escapeHtml(item.object.typequalname)}</span>
    </li>
  `).join('');
  document.getElementById(elementId).innerHTML = `
    <h2>${title} (${data.count})</h2>
    <ul class="refs">${items || '<li class="empty">No entries</li>'}</ul>
  `;
}

function renderSearchResults(items) {
  const el = document.getElementById('search-results');
  if (!items.length) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = `
    <h2>Type Search</h2>
    <ul class="refs">
      ${items.map(item => `<li>${objectLink({id: item.type_id, label: item.label})} <span class="type">${escapeHtml(item.qualname)}</span> <span class="edge">instances=${item.instance_count}</span></li>`).join('')}
    </ul>
  `;
}

function renderPathGroup(title, items) {
  if (!items.length) {
    return `<div class="path-group"><h3>${title}</h3><div class="empty">No paths found</div></div>`;
  }
  const rendered = items.map(path => `
    <li class="path-row">
      ${path.map(step => `${objectLink(step.object)} <span class="edge">${escapeHtml(step.ref)}</span>`).join('')}
    </li>
  `).join('');
  return `
    <div class="path-group">
      <h3>${title} (${items.length})</h3>
      <ul class="refs">${rendered}</ul>
    </div>
  `;
}

function renderPaths(modulePaths, framePaths) {
  document.getElementById('paths-panel').innerHTML = `
    <h2>Root Paths</h2>
    ${renderPathGroup('Module Paths', modulePaths.items)}
    ${renderPathGroup('Frame Paths', framePaths.items)}
  `;
}

function setMessage(message) {
  document.getElementById('message').textContent = message || '';
}

function setLoading(isLoading) {
  if (isLoading) {
    state.loadingCount += 1;
  } else {
    state.loadingCount = Math.max(0, state.loadingCount - 1);
  }
  const el = document.getElementById('loading');
  if (!el) {
    return;
  }
  if (state.loadingCount > 0) {
    el.classList.remove('hidden');
    el.innerHTML = '<span class="spinner" aria-hidden="true"></span><span>Loading…</span>';
  } else {
    el.classList.add('hidden');
    el.innerHTML = '';
  }
}

async function loadObject(id, pushState = true) {
  try {
    setMessage('');
    const [obj, referents, referrers, modulePaths, framePaths, marks] = await Promise.all([
      fetchJson(`/api/object?id=${encodeURIComponent(id)}`),
      fetchJson(`/api/referents?id=${encodeURIComponent(id)}&limit=100`),
      fetchJson(`/api/referrers?id=${encodeURIComponent(id)}&limit=100`),
      fetchJson(`/api/path-to-module?id=${encodeURIComponent(id)}&limit=10`),
      fetchJson(`/api/path-to-frame?id=${encodeURIComponent(id)}&limit=10`),
      fetchJson('/api/marks')
    ]);
    state.currentObjectId = obj.id;
    setObjectMode(true);
    renderObjectPanel(obj);
    renderMarksPanel(marks);
    renderRefs('outbound-panel', 'Outbound References', referents);
    renderRefs('inbound-panel', 'Inbound References', referrers);
    renderPaths(modulePaths, framePaths);
    if (pushState) {
      history.pushState({ id: obj.id }, '', `/?id=${obj.id}`);
    }
  } catch (err) {
    setMessage(err.message);
  }
}

function showLandingPage() {
  state.currentObjectId = null;
  setObjectMode(false);
  document.getElementById('object-panel').innerHTML = '';
  document.getElementById('inbound-panel').innerHTML = '';
  document.getElementById('outbound-panel').innerHTML = '';
  document.getElementById('paths-panel').innerHTML = '';
}

async function loadRootSummary(sampleSize = 200, topN = 10) {
  renderRootSummaryLoading(sampleSize);
  try {
    const rootSummary = await fetchJson(`/api/root-summary?sample_size=${encodeURIComponent(sampleSize)}&top_n=${encodeURIComponent(topN)}`);
    renderRootSummary(rootSummary);
  } catch (err) {
    renderRootSummaryError(err.message);
  }
}

async function init() {
  const [summary, topTypes, largestObjects] = await Promise.all([
    fetchJson('/api/summary'),
    fetchJson('/api/top-types?limit=12'),
    fetchJson('/api/largest-objects?limit=12')
  ]);
  renderSummary(summary);
  renderDiscovery(topTypes, largestObjects);
  loadRootSummary();
  showLandingPage();

  const params = new URLSearchParams(window.location.search);
  const id = params.get('id');
  const path = params.get('path');
  if (id) {
    await loadObject(id, false);
  } else if (path) {
    try {
      const payload = await fetchJson(`/api/go?path=${encodeURIComponent(path)}`);
      await loadObject(payload.id, false);
    } catch (err) {
      setMessage(err.message);
    }
  }

  document.body.addEventListener('click', async (event) => {
    const reloadButton = event.target.closest('#reload-root-summary');
    if (reloadButton) {
      event.preventDefault();
      loadRootSummary();
      return;
    }
    const button = event.target.closest('[data-object-id]');
    if (button) {
      event.preventDefault();
      loadObject(button.dataset.objectId);
      return;
    }
    const pathButton = event.target.closest('[data-go-path]');
    if (pathButton) {
      event.preventDefault();
      try {
        const payload = await fetchJson(`/api/go?path=${encodeURIComponent(pathButton.dataset.goPath)}`);
        loadObject(payload.id);
      } catch (err) {
        setMessage(err.message);
      }
    }
  });

  document.getElementById('jump-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const value = document.getElementById('jump-id').value.trim();
    if (value) loadObject(value);
  });

  document.getElementById('path-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = document.getElementById('path-input').value.trim();
    if (!value) return;
    try {
      const payload = await fetchJson(`/api/go?path=${encodeURIComponent(value)}`);
      loadObject(payload.id);
    } catch (err) {
      setMessage(err.message);
    }
  });

  document.getElementById('type-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = document.getElementById('type-input').value.trim();
    if (!value) return;
    try {
      const payload = await fetchJson(`/api/type-search?q=${encodeURIComponent(value)}`);
      renderSearchResults(payload.items);
    } catch (err) {
      setMessage(err.message);
    }
  });

  document.getElementById('random-btn').addEventListener('click', async () => {
    const payload = await fetchJson('/api/random');
    loadObject(payload.id);
  });

  document.body.addEventListener('submit', async (event) => {
    if (event.target.id !== 'mark-form') {
      return;
    }
    event.preventDefault();
    const input = document.getElementById('mark-input');
    const mark = input.value.trim();
    if (!mark || !state.currentObjectId) {
      return;
    }
    try {
      await fetchJson(`/api/mark?id=${encodeURIComponent(state.currentObjectId)}&mark=${encodeURIComponent(mark)}`);
      input.value = '';
      await loadObject(state.currentObjectId, false);
    } catch (err) {
      setMessage(err.message);
    }
  });

  window.addEventListener('popstate', (event) => {
    if (event.state && event.state.id) {
      loadObject(event.state.id, false);
      return;
    }
    const params = new URLSearchParams(window.location.search);
    const id = params.get('id');
    if (id) {
      loadObject(id, false);
    } else {
      showLandingPage();
    }
  });
}

init().catch(err => setMessage(err.message));
"""


STYLES_CSS = """body {
  font-family: ui-sans-serif, system-ui, sans-serif;
  margin: 0;
  background: #f3f0e8;
  color: #1f2328;
}
.topbar {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  padding: 0.75rem 1rem;
  background: #1f3a5f;
  color: #fff;
  position: sticky;
  top: 0;
}
.topbar form { display: flex; gap: 0.4rem; }
.topbar input {
  min-width: 14rem;
  padding: 0.45rem 0.6rem;
}
.topbar button, .object-link {
  padding: 0.45rem 0.7rem;
  border: 0;
  background: #d6c5a3;
  color: #1f2328;
  cursor: pointer;
  text-decoration: none;
  display: inline-block;
}
.brand { font-weight: 700; margin-right: 0.5rem; }
.brand {
  color: inherit;
  text-decoration: none;
}
.summary, .message, .panel {
  margin: 1rem;
  padding: 1rem;
  background: #fffdf8;
  border: 1px solid #d7d1c3;
  border-radius: 0.5rem;
}
.loading {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  margin: 1rem;
  padding: 0.75rem 1rem;
  background: #f9f1df;
  border: 1px solid #d7c5a1;
  border-radius: 0.5rem;
  color: #7a4b10;
}
.hidden {
  display: none;
}
.spinner {
  width: 0.95rem;
  height: 0.95rem;
  border: 2px solid #d7c5a1;
  border-top-color: #8a4b08;
  border-radius: 999px;
  animation: spin 0.85s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}
.layout {
  display: grid;
  grid-template-columns: 1.2fr 1fr 1.2fr;
  gap: 0;
}
body.landing-mode .layout,
body.landing-mode #paths-panel {
  display: none;
}
body.object-mode #discovery-panel,
body.object-mode #root-summary-panel,
body.object-mode #marks-panel {
  display: none;
}
body.landing-mode #search-results:empty {
  display: none;
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.75rem;
}
.panel-header h2 {
  margin: 0;
}
.subtle {
  color: #5d6470;
  margin-bottom: 0.9rem;
}
.loading-inline {
  display: flex;
  align-items: center;
  gap: 0.65rem;
}
.discovery-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
}
.refs {
  list-style: none;
  padding: 0;
  margin: 0;
}
.refs li {
  padding: 0.35rem 0;
  border-top: 1px solid #eee6d7;
}
.refs li:first-child { border-top: 0; }
.edge {
  font-family: ui-monospace, monospace;
  color: #8a4b08;
  margin-right: 0.45rem;
}
.type {
  color: #5d6470;
  margin-left: 0.45rem;
}
.path-link {
  padding: 0.12rem 0.28rem;
  background: #efe3c9;
}
.path-sep {
  color: #8a4b08;
  margin: 0 0.12rem;
}
.meta {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.35rem 0.75rem;
}
.object-label {
  font-family: ui-monospace, monospace;
  font-weight: 700;
  margin-bottom: 0.75rem;
}
.mark-form {
  display: flex;
  gap: 0.4rem;
  margin-bottom: 0.75rem;
}
.mark-form input {
  flex: 1;
  padding: 0.45rem 0.6rem;
}
.mark-list {
  margin-bottom: 0.75rem;
}
.mark-chip {
  display: inline-block;
  padding: 0.2rem 0.45rem;
  margin-right: 0.35rem;
  background: #e8dcc5;
  border-radius: 999px;
  font-size: 0.9rem;
}
.empty { color: #6b7280; }
.path-group + .path-group { margin-top: 1rem; }
.path-row {
  line-height: 1.8;
  word-break: break-word;
}
@media (max-width: 980px) {
  .layout { grid-template-columns: 1fr; }
  .discovery-grid { grid-template-columns: 1fr; }
  .topbar { flex-wrap: wrap; }
  .topbar input { min-width: 10rem; }
}
"""


def _json_bytes(payload):
    return json.dumps(payload, sort_keys=True).encode('utf-8')


def dispatch_request(db_path, path):
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    if parsed.path == '/':
        return 200, 'text/html; charset=utf-8', INDEX_HTML.encode('utf-8')
    if parsed.path == '/app.js':
        return 200, 'application/javascript; charset=utf-8', APP_JS.encode('utf-8')
    if parsed.path == '/styles.css':
        return 200, 'text/css; charset=utf-8', STYLES_CSS.encode('utf-8')

    try:
        with Reader(db_path) as reader:
            if parsed.path == '/api/summary':
                return 200, 'application/json; charset=utf-8', _json_bytes(reader.summary_stats())
            if parsed.path == '/api/marks':
                return 200, 'application/json; charset=utf-8', _json_bytes(
                    {'items': reader.get_all_marks()}
                )
            if parsed.path == '/api/random':
                return 200, 'application/json; charset=utf-8', _json_bytes({'id': reader.random_object_id()})
            if parsed.path == '/api/object':
                return 200, 'application/json; charset=utf-8', _json_bytes(
                    reader.object_summary(_required_int(query, 'id'))
                )
            if parsed.path == '/api/mark':
                reader.mark_object(
                    _required_int(query, 'id'),
                    _required_param(query, 'mark'),
                )
                reader.conn.commit()
                return 200, 'application/json; charset=utf-8', _json_bytes({'ok': True})
            if parsed.path == '/api/referents':
                return 200, 'application/json; charset=utf-8', _json_bytes(
                    reader.object_referents_data(
                        _required_int(query, 'id'),
                        limit=_int_param(query, 'limit', 50),
                    )
                )
            if parsed.path == '/api/referrers':
                return 200, 'application/json; charset=utf-8', _json_bytes(
                    reader.object_referrers_data(
                        _required_int(query, 'id'),
                        limit=_int_param(query, 'limit', 50),
                    )
                )
            if parsed.path == '/api/type-search':
                query_text = _required_param(query, 'q')
                return 200, 'application/json; charset=utf-8', _json_bytes(
                    {'items': reader.type_search_data(query_text, limit=_int_param(query, 'limit', 20))}
                )
            if parsed.path == '/api/top-types':
                return 200, 'application/json; charset=utf-8', _json_bytes(
                    {'items': reader.top_types_data(limit=_int_param(query, 'limit', 20))}
                )
            if parsed.path == '/api/largest-objects':
                return 200, 'application/json; charset=utf-8', _json_bytes(
                    {'items': reader.largest_objects_data(limit=_int_param(query, 'limit', 20))}
                )
            if parsed.path == '/api/root-summary':
                return 200, 'application/json; charset=utf-8', _json_bytes(
                    reader.sampled_root_summary_data(
                        sample_size=_int_param(query, 'sample_size', 500),
                        top_n=_int_param(query, 'top_n', 10),
                    )
                )
            if parsed.path == '/api/path-to-module':
                return 200, 'application/json; charset=utf-8', _json_bytes(
                    {'items': reader.path_to_module_data(
                        _required_int(query, 'id'),
                        limit=_int_param(query, 'limit', 20),
                    )}
                )
            if parsed.path == '/api/path-to-frame':
                return 200, 'application/json; charset=utf-8', _json_bytes(
                    {'items': reader.path_to_frame_data(
                        _required_int(query, 'id'),
                        limit=_int_param(query, 'limit', 20),
                    )}
                )
            if parsed.path == '/api/go':
                try:
                    obj_id = reader.resolve_path(_required_param(query, 'path'))
                except PathFailure as exc:
                    return 404, 'application/json; charset=utf-8', _json_bytes({'error': str(exc)})
                return 200, 'application/json; charset=utf-8', _json_bytes({'id': obj_id})
    except Exception as exc:
        return 400, 'application/json; charset=utf-8', _json_bytes({'error': str(exc)})

    return 404, 'application/json; charset=utf-8', _json_bytes({'error': 'not found'})


def _required_param(query, name):
    values = query.get(name)
    if not values or not values[0]:
        raise ValueError('missing query parameter: {}'.format(name))
    return values[0]


def _required_int(query, name):
    return int(_required_param(query, name))


def _int_param(query, name, default):
    values = query.get(name)
    if not values or not values[0]:
        return default
    return int(values[0])


def make_handler(db_path):
    class ObjexWebHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            status_code, content_type, payload = dispatch_request(db_path, self.path)
            self.send_response(status_code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ObjexWebHandler


def make_server(db_path, host='127.0.0.1', port=8000):
    return ThreadingHTTPServer((host, port), make_handler(db_path))


def serve(db_path, host='127.0.0.1', port=8000):
    server = make_server(db_path, host=host, port=port)
    return server
