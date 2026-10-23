// Build a standalone page that runs a Canvas subtitle preset with the EXACT runtime of the Power Tools Hub
// (canvasDoc from skillhub/index.html, the file next to this tools folder; only read, never changed) + a driver for
// frame-by-frame rendering.
// node build_page.js <preset.json> <out.html> [W H] [params-json]
//
// The preset code is untrusted (anyone can write to the shared hub database), so the page fences it in like the hub does:
//  - the preset document (canvasDoc, byte for byte as the hub builds it) runs inside <iframe sandbox="allow-scripts" srcdoc>:
//    opaque origin, no access to the host page, storage, cookies, popups, forms or top navigation;
//  - the host page relays words / params / time with postMessage, exactly like the hub's buildCanvasFrame / canvasFrame;
//  - both documents carry a CSP that allows inline code and Google Fonts only (no fetch / XHR / WebSocket / images / frames);
//    preview.py also cuts the browser's network and kills the browser when a frame hangs.
// Small additions around the runtime (the runtime text itself is untouched): a CSP <meta> in the preset document's head, and a
// listener before / after the runtime's own that answers every drawn frame with {drawn: t, ms} (ms = the runtime's draw: clear +
// the preset code, measured inside the sandbox), so the host knows the frame is on the canvas and how long it took.
const fs = require('fs');
const path = require('path');
const HUB = path.join(__dirname, '..', 'index.html');
const [, , presetPath, outPath, Ws, Hs, paramsJson, wordsPath] = process.argv;   // wordsPath: real word times (tools/hub_subs.py)
const s = fs.readFileSync(HUB, 'utf8');
function cut(from, to, what) {
  const a = s.indexOf(from), b = to == null ? -1 : s.indexOf(to, a);
  if (a < 0 || (to != null && b < 0)) throw new Error(`build_page: ${what} not found in ${HUB} (looked for "${a < 0 ? from : to}")`);
  return [a, b];
}
const [a, b] = cut('const CANVAS_FONT_RE', 'function buildCanvasFrame', 'the canvas runtime');
const canvasDoc = new Function(s.slice(a, b) + '; return canvasDoc;')();
const [t0] = cut('const SAMPLE_TEXT', null, 'SAMPLE_TEXT'), t1 = s.indexOf('\n', t0);
const sample = new Function(s.slice(t0, t1) + '; return SAMPLE_TEXT;')();
const [w0] = cut('function timeWords(', null, 'timeWords()'), w1 = s.indexOf('\n}\n', w0) + 3;
const timeWords = new Function(s.slice(w0, w1) + '; return timeWords;')();

let raw = JSON.parse(fs.readFileSync(presetPath, 'utf8'));
if (raw.data && !raw.code) raw = raw.data;
const code = Array.isArray(raw.code) ? raw.code.join('\n') : raw.code;
const doc = canvasDoc({ code, fonts: raw.fonts || [] });
let tw = timeWords(sample);
let words = tw.words.map((w, i) => ({ text: w, start: tw.times[i][0], end: tw.times[i][1] }));
if (wordsPath) {   // [[word, start, end], ...] (tools/align_words.py) or [{text, start, end}, ...]
  const rows = JSON.parse(fs.readFileSync(wordsPath, 'utf8'));
  words = (Array.isArray(rows) ? rows : rows.words || []).map(r => Array.isArray(r) ? { text: String(r[0]), start: +r[1], end: +r[2] } : { text: String(r.text || r.word), start: +r.start, end: +r.end });
  const total = words.reduce((m, w) => Math.max(m, w.end), 0) + 1;
  tw = { words: words.map(w => w.text), times: words.map(w => [w.start, w.end]), total };
}
const params = {};
for (const q of raw.params || []) params[q.name] = q.value;
Object.assign(params, paramsJson ? JSON.parse(paramsJson) : {});
const W = +Ws || 1080, H = +Hs || 1920;
const SC = 'scr' + 'ipt';

const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; " +
  "font-src https://fonts.gstatic.com; img-src data: blob:";
const cspMeta = `<meta http-equiv="Content-Security-Policy" content="${CSP}">`;
// JSON inside an inline script of the host page: params come from the preset, so no '</script>' / '<!--' can break out
const js = v => JSON.stringify(v).replace(/</g, '\\u003c');   // (U+2028/2029 are legal in JS strings since ES2019)
const attr = v => v.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// --- the preset document: the hub's canvasDoc + CSP meta (head) + a frame clock around the runtime's own message listener:
// 'message' listeners on window run in the order they were added, so one added BEFORE the runtime starts the clock and one
// added AFTER it (i.e. after the frame was drawn) answers the host with {drawn: t, ms}
const HEAD = '<head><meta charset="utf-8">', BODY = '<body><canvas id="c"></canvas>';
const hi = doc.indexOf(HEAD), bi = doc.indexOf(BODY), di = doc.indexOf('window.__draw=function(ctx,s){');
if (hi < 0 || hi > 40 || bi < 0 || di < 0 || bi > di)
  throw new Error('build_page: canvasDoc no longer looks like <head><meta charset> ... ' + BODY + ' runtime, preset code - update build_page.js');
const isFrame = "function (e) { var m = e.data; return e.source === parent && !!m && m.hubCanvasIn === 1 && typeof m.t === 'number'; }";
const start = `<${SC}>(function () {
  var clock = { t0: 0 }, isFrame = ${isFrame};
  Object.defineProperty(window, '__pvClock', { value: clock });
  addEventListener('message', function (e) { if (isFrame(e)) clock.t0 = performance.now(); });
})();</${SC}>`;
const reply = `<${SC}>(function () {
  var clock = window.__pvClock, isFrame = ${isFrame};
  addEventListener('message', function (e) { if (isFrame(e)) parent.postMessage({ hubCanvas: 1, drawn: e.data.t, ms: performance.now() - clock.t0 }, '*'); });
})();</${SC}>`;
const be = doc.lastIndexOf('</body>');          // the LAST one: the preset code may contain the text '</body>' itself
if (be < 0) throw new Error('build_page: canvasDoc has no </body>');
const at = bi + '<body>'.length;
const inner = doc.slice(0, hi + HEAD.length) + cspMeta + doc.slice(hi + HEAD.length, at) + start + doc.slice(at, be) + reply + doc.slice(be);

// --- the host page: driver + sandboxed iframe (driver first, so it listens before the iframe can say 'ready')
const driver = `<${SC}>
(function () {
  var frame = function () { return document.getElementById('cv'); }, waiting = null;
  window.__errs = []; window.__ready = false; window.__total = ${js(tw.total)};
  window.addEventListener('message', function (e) {
    var f = frame(); if (!f || e.source !== f.contentWindow) return;
    var m = e.data; if (!m || m.hubCanvas !== 1) return;
    if (m.ready) window.__ready = true;
    if (m.err) window.__errs.push(String(m.err));
    if (typeof m.drawn === 'number' && waiting && waiting.t === m.drawn) { var w = waiting; waiting = null; w.res(+m.ms || 0); }
  });
  window.__init = function () {
    frame().contentWindow.postMessage({ hubCanvasIn: 1, words: ${js(words)}, total: ${js(tw.total)}, W: ${W}, H: ${H}, params: ${js(params)} }, '*');
  };
  // draw the frame at t inside the sandbox; resolves with the draw time in ms once it is on the canvas
  window.__render = function (t) {
    return new Promise(function (res) { waiting = { t: t, res: res }; frame().contentWindow.postMessage({ hubCanvasIn: 1, t: t }, '*'); });
  };
  // timing pass, started without waiting for it; preview.py polls __tstat (i = frames done) so its watchdog sees every frame.
  // draw = JS time of each frame inside the sandbox; cycle = wall time per frame of the back-to-back loop, which also contains
  // the browser rendering / painting the previous frame before the next message is handled
  window.__timing = function (total, n) {
    var st = window.__tstat = { i: 0, n: n, t: 0, draw: [], cycle: 0, done: false }, t0 = performance.now();
    (async function () {
      for (var i = 0; i < n; i++) { st.t = total * i / n; st.draw.push(await window.__render(st.t)); st.i = i + 1; }
      st.cycle = (performance.now() - t0) / n; st.done = true;
    })();
  };
})();
</${SC}>`;
const host = '<!doctype html><html><head><meta charset="utf-8">' + cspMeta + '<title>Canvas preset preview</title>' +
  '<style>:root{color-scheme:light}html,body{margin:0;padding:0;height:100%;background:transparent;overflow:hidden}' +
  '#cv{position:fixed;left:0;top:0;width:100%;height:100%;border:0;display:block;background:transparent;color-scheme:light}</style></head>' +
  `<body>${driver}<iframe id="cv" sandbox="allow-scripts" title="Subtitle preview" srcdoc="${attr(inner)}"></iframe></body></html>`;
fs.writeFileSync(outPath, host);
console.log(JSON.stringify({ total: tw.total, words: words.length, w: tw.words, s: tw.times.map(x => x[0]) }));
