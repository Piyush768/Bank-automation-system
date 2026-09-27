"""In-page JavaScript used by BrowserSurface.

Perception deliberately does NOT depend on IDs, test IDs, <label for>, or ARIA
being present (legacy apps rarely have them). It describes each control the
way an operator would: its kind, the text beside it, the table row it sits in
and that column's header, and where it is on screen.
"""

LIB = r"""
const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
const key = (t) => norm(t).toLowerCase().replace(/[:\s]+$/, '');
const vis = (e) => {
  const r = e.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  const s = getComputedStyle(e);
  return s.visibility !== 'hidden' && s.display !== 'none';
};
const TEXT_INPUT = ['text','password','search','email','tel','number','date',''];
const isClickable = (e) => {
  const t = e.tagName.toLowerCase();
  if (t === 'a' && e.hasAttribute('href')) return true;
  if (t === 'button') return true;
  if (t === 'input' && ['submit','button','image','reset','checkbox','radio'].includes((e.type||'').toLowerCase())) return true;
  if (e.hasAttribute('onclick')) return true;
  const r = e.getAttribute('role');
  if (r === 'button' || r === 'link' || r === 'menuitem' || r === 'tab') return true;
  if (getComputedStyle(e).cursor === 'pointer') {
    const p = e.parentElement;
    if (!p || getComputedStyle(p).cursor !== 'pointer') return true;
  }
  return false;
};
const insideClickable = (e) => {
  let p = e.parentElement;
  while (p && p !== document.body) { if (isClickable(p)) return true; p = p.parentElement; }
  return false;
};
const controlOf = (e) => {
  const t = e.tagName.toLowerCase();
  if (t === 'select') return 'select';
  if (t === 'textarea') return 'input';
  if (t === 'input' && TEXT_INPUT.includes((e.getAttribute('type')||'text').toLowerCase())) return 'input';
  if (t === 'input' && (e.type||'') === 'hidden') return null;
  if (isClickable(e) && !insideClickable(e)) return 'clickable';
  return null;
};
const isTextLeaf = (e) => {
  if (e.children.length !== 0) return false;
  if (['script','style','option','title'].includes(e.tagName.toLowerCase())) return false;
  const t = norm(e.innerText);
  return t.length > 0 && t.length <= 120 && !insideClickable(e) && !isClickable(e);
};
const matchesControl = (e, want) => {
  if (!vis(e)) return false;
  if (want === 'text') return norm(e.innerText).length > 0;
  return controlOf(e) === want;
};
const ownText = (e) => {
  const t = e.tagName.toLowerCase();
  if (t === 'input') return norm(e.value || e.getAttribute('value') || '');
  return norm(e.innerText);
};
const roleOf = (e) => {
  const r = e.getAttribute('role'); if (r) return r;
  const t = e.tagName.toLowerCase();
  if (t === 'a' && e.hasAttribute('href')) return 'link';
  if (t === 'button') return 'button';
  if (t === 'select') return 'combobox';
  if (t === 'input') {
    const ty = (e.type || 'text').toLowerCase();
    if (['submit','button','reset','image'].includes(ty)) return 'button';
    if (['text','search','email','tel',''].includes(ty)) return 'textbox';
    if (ty === 'checkbox' || ty === 'radio') return ty;
  }
  return '';
};
const accName = (e) => {
  const al = e.getAttribute('aria-label'); if (al) return norm(al);
  if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) return norm(l.innerText); }
  const t = e.tagName.toLowerCase();
  if (t === 'input' && ['submit','button','reset'].includes((e.type||'').toLowerCase())) return norm(e.value);
  if (t === 'a' || t === 'button') return norm(e.innerText);
  return norm(e.getAttribute('title') || '');
};
const labelFor = (e) => {
  if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) return norm(l.innerText); }
  const al = e.getAttribute('aria-label'); if (al) return norm(al);
  const cell = e.closest('td,th');
  if (cell) {
    let p = cell.previousElementSibling;
    while (p) { const t = norm(p.innerText); if (t) return t; p = p.previousElementSibling; }
  }
  let n = e.previousSibling;
  while (n) { const t = norm(n.textContent); if (t) return t.slice(-40); n = n.previousSibling; }
  return norm(e.getAttribute('placeholder') || '');
};
const isHeaderRow = (tr) => {
  if (!tr) return false;
  if (tr.querySelector('th')) return true;
  if (tr.hasAttribute('bgcolor')) return true;
  const cells = Array.from(tr.cells || []);
  return cells.length > 1 && cells.every(c => c.querySelector('b,strong') && norm(c.innerText) === norm((c.querySelector('b,strong')||{}).innerText));
};
const cellInfo = (e) => {
  const cell = e.closest('td,th'); const tr = e.closest('tr'); const table = e.closest('table');
  if (!cell || !tr || !table) return {row_cells: [], cell_index: -1, column_header: '', row_text: ''};
  const cells = Array.from(tr.cells).map(c => norm(c.innerText));
  const idx = Array.from(tr.cells).indexOf(cell);
  const first = table.rows[0];
  let header = '';
  if (first && first !== tr && isHeaderRow(first) && first.cells[idx]) header = norm(first.cells[idx].innerText);
  return {row_cells: cells, cell_index: idx, column_header: header, row_text: cells.join(' | ')};
};
const cssPath = (e) => {
  const parts = []; let n = e;
  while (n && n.nodeType === 1 && n.tagName.toLowerCase() !== 'html') {
    let i = 1, s = n; while ((s = s.previousElementSibling)) if (s.tagName === n.tagName) i++;
    parts.unshift(`${n.tagName.toLowerCase()}:nth-of-type(${i})`); n = n.parentElement;
  }
  return parts.join(' > ');
};
const effectText = (e) => {
  const c = controlOf(e);
  if (c === 'clickable') return ownText(e) || accName(e);
  if (c === 'input' && e.form) {
    const subs = Array.from(e.form.querySelectorAll('input[type=submit],button,[onclick]')).map(ownText).filter(Boolean);
    return subs.join(' / ');
  }
  return '';
};

// ---------- locator resolution (replay + record-time validation) ----------
const labelNodes = (label) => {
  const k = key(label); const out = [];
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e) || key(e.innerText) !== k) continue;
    if (Array.from(e.children).some(c => key(c.innerText) === k)) continue; // take deepest
    out.push(e);
  }
  return out;
};
const controlsIn = (root, want) => {
  const all = [root, ...root.querySelectorAll('*')];
  if (want === 'text') {
    const t = norm(root.innerText);
    return t ? [root] : [];
  }
  return all.filter(e => matchesControl(e, want));
};
const resolveStrategy = (s) => {
  let found = [];
  if (s.kind === 'label') {
    for (const ln of labelNodes(s.label)) {
      const cell = ln.closest('td,th');
      if (s.relation === 'same_row') {
        const tr = ln.closest('tr'); if (!tr) continue;
        for (const c of tr.cells) { if (c === cell) continue; found.push(...controlsIn(c, s.control)); }
      } else if (cell) {
        let p = cell.nextElementSibling;
        while (p) { const hits = controlsIn(p, s.control); if (hits.length) { found.push(...hits); break; } p = p.nextElementSibling; }
      } else {
        let p = ln.nextElementSibling;
        while (p) { const hits = controlsIn(p, s.control); if (hits.length) { found.push(...hits); break; } p = p.nextElementSibling; }
      }
    }
  } else if (s.kind === 'table_cell') {
    const rk = key(s.row_contains), ck = key(s.column);
    for (const table of document.querySelectorAll('table')) {
      const first = table.rows[0]; if (!first || !isHeaderRow(first)) continue;
      const col = Array.from(first.cells).findIndex(c => key(c.innerText) === ck);
      if (col < 0) continue;
      for (const tr of Array.from(table.rows).slice(1)) {
        if (tr.closest('table') !== table) continue;
        const cellsTxt = Array.from(tr.cells).map(c => key(c.innerText));
        if (!cellsTxt.some(t => t === rk || t.includes(rk))) continue;
        const cell = tr.cells[col]; if (!cell || !vis(cell)) continue;
        found.push(...controlsIn(cell, s.control));
      }
    }
  } else if (s.kind === 'text') {
    const k = key(s.text);
    for (const e of document.querySelectorAll('body *')) if (matchesControl(e, s.control) && key(ownText(e)) === k) found.push(e);
  } else if (s.kind === 'role') {
    for (const e of document.querySelectorAll('body *')) if (vis(e) && roleOf(e) === s.role && key(accName(e)) === key(s.name)) found.push(e);
  } else if (s.kind === 'css') {
    try { for (const e of document.querySelectorAll(s.selector)) if (matchesControl(e, s.control)) found.push(e); } catch (err) {}
  } else if (s.kind === 'coords') {
    const x = s.x * window.innerWidth / s.viewport_w, y = s.y * window.innerHeight / s.viewport_h;
    let e = document.elementFromPoint(x, y);
    while (e && e !== document.body && !matchesControl(e, s.control)) e = e.parentElement;
    if (e && e !== document.body) found.push(e);
  }
  return Array.from(new Set(found));
};
"""

OBSERVE = "(opts) => {" + LIB + r"""
  document.querySelectorAll('[data-cua-idx]').forEach(e => e.removeAttribute('data-cua-idx'));
  const out = []; let i = 0; let texts = 0;
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e)) continue;
    let c = controlOf(e);
    if (!c && isTextLeaf(e)) { if (texts >= opts.max_text) continue; c = 'text'; texts++; }
    if (!c) continue;
    const r = e.getBoundingClientRect();
    const ci = cellInfo(e);
    e.setAttribute('data-cua-idx', String(i));
    out.push({
      idx: i, control: c, tag: e.tagName.toLowerCase(),
      input_type: e.tagName.toLowerCase() === 'input' ? (e.type || 'text') : '',
      role: roleOf(e), name: accName(e),
      label: (c === 'clickable') ? '' : labelFor(e),
      text: c === 'input' ? '' : ownText(e).slice(0, 120),
      value: (c === 'input' && e.type !== 'password') ? norm(e.value).slice(0, 60) : '',
      row_text: ci.row_text.slice(0, 200), row_cells: ci.row_cells, cell_index: ci.cell_index,
      column_header: ci.column_header, effect_text: effectText(e),
      bbox: [r.x, r.y, r.width, r.height], css: cssPath(e),
    });
    i++;
  }
  const lm = [];
  for (const e of document.querySelectorAll('b,strong,h1,h2,h3,h4')) {
    const t = norm(e.innerText);
    if (vis(e) && t && t.length <= 80 && !lm.includes(t)) lm.push(t);
  }
  return {
    url: location.href, title: document.title, landmarks: lm.slice(0, 25), elements: out,
    page_text: norm(document.body ? document.body.innerText : '').slice(0, 5000),
    viewport: [window.innerWidth, window.innerHeight],
  };
}"""

LANDMARKS = "() => {" + LIB + r"""
  const lm = [];
  for (const e of document.querySelectorAll('b,strong,h1,h2,h3,h4')) {
    const t = norm(e.innerText); if (vis(e) && t && t.length <= 80 && !lm.includes(t)) lm.push(t);
  }
  return lm;
}"""

# Resolve one strategy; stamp matches with a token so Python can build a handle.
RESOLVE = "(args) => {" + LIB + r"""
  document.querySelectorAll('[data-cua-r]').forEach(e => e.removeAttribute('data-cua-r'));
  const found = resolveStrategy(args.strategy);
  found.forEach(e => e.setAttribute('data-cua-r', args.token));
  let sameAsIdx = null;
  if (args.idx !== null && args.idx !== undefined) {
    const orig = document.querySelector(`[data-cua-idx="${args.idx}"]`);
    sameAsIdx = found.length === 1 && !!orig && (found[0] === orig || found[0].contains(orig) || orig.contains(found[0]));
  }
  return {count: found.length, same: sameAsIdx};
}"""

# Reports raw input events; Python keeps only those that happen while an
# operator holds control (automation's own events are ignored by owner).
HUMAN_CAPTURE = r"""
(() => {
  if (window.__cuaCaptureInstalled) return; window.__cuaCaptureInstalled = true;
  const norm = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const desc = (e) => {
    const t = e.tagName.toLowerCase();
    const txt = t === 'input' ? (e.type === 'password' ? '' : (e.value || '')) : norm(e.innerText);
    const cell = e.closest('td'); let label = '';
    if (cell && cell.previousElementSibling) label = norm(cell.previousElementSibling.innerText);
    return {tag: t, type: e.type || '', text: txt.slice(0, 60), label: label.slice(0, 60)};
  };
  document.addEventListener('click', (ev) => {
    if (window.__cuaHuman) window.__cuaHuman({kind: 'click', target: desc(ev.target)});
  }, true);
  document.addEventListener('change', (ev) => {
    if (window.__cuaHuman) window.__cuaHuman({kind: 'input', target: {...desc(ev.target), text: ''}, value_len: (ev.target.value || '').length});
  }, true);
})();
"""
