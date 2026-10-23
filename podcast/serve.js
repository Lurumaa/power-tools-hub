// Local server for ../editor: static files with HTTP Range (video seeking) + local rendering on this machine.
//   node serve.js [port]
// POST /api/render        editor state (JSON) -> starts podcast/render.py (ffmpeg, NVENC GPU when available) -> {id}
//                         files land in editor/exports/<task>/ (task = the name without its variant: task_2004_3 ->
//                         task_2004); cleanName = the bundle's clean file in the same run; drive = also copy the
//                         finished files into the Drive "Upload" folder (exact names, replaces what is there)
// GET  /api/render/<id>   {state: running|done|error, progress 0..1, encoder, file, files, url, drive, error}
// GET  /api/drive?names=a.mp4,b.mp4   {dir, ok, exists:[names already in Upload]}
// POST /api/upload?name=  raw file body (a file imported into the editor from disk) -> {src} under editor/uploads/
// GET  /api/fonts         every installed TTF/OTF (Windows + per-user fonts): [{id, family, style, weight, italic}]
// GET  /api/font/<id>     that font file (the editor loads it with FontFace, like AE uses local fonts)
const http = require('http'), fs = require('fs'), path = require('path'), { spawn } = require('child_process');
const ROOT = path.resolve(__dirname, '..', 'editor'), PORT = +process.argv[2] || 8765;
const EXPORTS = path.join(ROOT, 'exports'), UPLOADS = path.join(ROOT, 'uploads');
const RENDER = path.join(__dirname, 'render.py'), PYTHON = process.env.POD_PYTHON || 'python';
const TYPES = { '.html':'text/html; charset=utf-8', '.js':'text/javascript', '.json':'application/json', '.css':'text/css',
  '.mp4':'video/mp4', '.webm':'video/webm', '.mov':'video/quicktime', '.mp3':'audio/mpeg', '.wav':'audio/wav',
  '.png':'image/png', '.jpg':'image/jpeg', '.webp':'image/webp' };
const jobs = new Map();
// the Drive for Desktop mount of the shared "Upload" folder (Ffmpeg/Upload) where finished creatives are delivered
const UPLOAD_DIR = process.env.EDITOR_UPLOAD_DIR || '';   // the team's delivery folder: set EDITOR_UPLOAD_DIR
// one folder per task: "task_2004_3" -> "task_2004" (the trailing variant goes), a one-part name stays as it is
const taskDir = n => { const t = safe(n).replace(/^(hook|body)b?_/i, ''); return t.split('_').length > 2 ? t.replace(/_[^_]+$/, '') : t; };
const safe = s => String(s || 'untitled').replace(/[^\w.\-]+/g, '_').slice(0, 80) || 'untitled';
const send = (res, code, obj) => { res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(obj)); };
const readBody = req => new Promise((ok, bad) => { const c = []; req.on('data', d => c.push(d)); req.on('end', () => ok(Buffer.concat(c))); req.on('error', bad); });

// an editor media src ("videos/x/y.mp4", "uploads/z.mp4", "/videos/...") -> a file inside the editor folder
function mediaPath(src) {
  const rel = decodeURIComponent(String(src).split('?')[0].split('#')[0]).replace(/^\/+/, '');
  const f = path.resolve(ROOT, rel);
  if (!f.startsWith(ROOT) || !fs.existsSync(f)) throw new Error(`media not found on disk: ${src}`);
  return f;
}

async function startRender(req, res) {
  let state;
  try { state = JSON.parse((await readBody(req)).toString('utf8')); } catch (e) { return send(res, 400, { error: 'bad JSON' }); }
  try {
    const dir = taskDir(state.task || state.name), outDir = path.join(EXPORTS, dir);
    fs.mkdirSync(outDir, { recursive: true });
    const id = Date.now().toString(36);
    // local files never overwrite: name.mp4, name_v2.mp4 ... (both bundle files get the same suffix)
    const names = [safe(state.name)].concat(state.cleanName ? [safe(state.cleanName)] : []);
    let sfx = '', k = 2;
    while (names.some(n => fs.existsSync(path.join(outDir, `${n}${sfx}.mp4`)))) sfx = `_v${k++}`;
    const file = `${names[0]}${sfx}.mp4`, cleanFile = state.cleanName ? `${names[1]}${sfx}.mp4` : null;
    const job = { ...state, media: state.media.map(m => ({ ...m, path: mediaPath(m.src) })),
      subs: (state.subs || []).map(s => ({ ...s, fontFile: fontFile(s.st) })),
      layers: (state.layers || []).map(l => ({ ...l, path: mediaPath(l.src) })),
      out: path.join(outDir, file), clean_out: cleanFile ? path.join(outDir, cleanFile) : null,
      status: path.join(EXPORTS, `.${id}.status.json`) };
    const jobFile = path.join(EXPORTS, `.${id}.job.json`);
    fs.writeFileSync(jobFile, JSON.stringify(job));
    fs.writeFileSync(job.status, JSON.stringify({ state: 'running', progress: 0 }));
    const p = spawn(PYTHON, [RENDER, jobFile], { cwd: __dirname, windowsHide: true });
    let err = ''; p.stderr.on('data', d => { err = (err + d).slice(-2000); });
    const files = [file].concat(cleanFile ? [cleanFile] : []);
    const deliver = names.map(n => `${n}.mp4`);           // Drive gets the exact names (no _v2)
    p.on('exit', async code => {
      let s = {}; try { s = JSON.parse(fs.readFileSync(job.status, 'utf8')); } catch (e) {}
      if (s.state !== 'done' && s.state !== 'error') { fs.writeFileSync(job.status, JSON.stringify({ ...s, state: 'error', error: err || `render exited with ${code}` })); return; }
      if (s.state === 'done' && state.drive) {
        const put = d => fs.writeFileSync(job.status, JSON.stringify({ ...s, drive: d }));
        put('copying');
        try {
          if (!fs.existsSync(UPLOAD_DIR)) throw new Error(`Drive folder not found: ${UPLOAD_DIR}`);
          for (let i = 0; i < files.length; i++) await fs.promises.copyFile(path.join(outDir, files[i]), path.join(UPLOAD_DIR, deliver[i]));
          put('copied'); console.log(`drive <- ${deliver.join(' + ')}`);
        } catch (e) { put('error: ' + e.message); }
      }
    });
    jobs.set(id, { status: job.status, file, files, dir, deliver });
    console.log(`render ${id} -> exports/${dir}/${files.join(' + ')}`);
    send(res, 200, { id, file, files, dir });
  } catch (e) { send(res, 400, { error: e.message }); }
}

function renderStatus(id, res) {
  const j = jobs.get(id); if (!j) return send(res, 404, { error: 'unknown render job' });
  let s = { state: 'running', progress: 0 }; try { s = JSON.parse(fs.readFileSync(j.status, 'utf8')); } catch (e) {}
  send(res, 200, { ...s, file: j.file, files: j.files, dir: j.dir, deliver: j.deliver,
    url: `exports/${encodeURIComponent(j.dir)}/${encodeURIComponent(j.file)}`,
    urls: j.files.map(f => `exports/${encodeURIComponent(j.dir)}/${encodeURIComponent(f)}`) });
}
function driveCheck(url, res) {
  const names = (url.searchParams.get('names') || '').split(',').filter(Boolean).map(n => safe(n.replace(/\.mp4$/i, '')) + '.mp4');
  const ok = fs.existsSync(UPLOAD_DIR);
  send(res, 200, { dir: UPLOAD_DIR, ok, exists: ok ? names.filter(n => fs.existsSync(path.join(UPLOAD_DIR, n))) : [] });
}

// ---- local fonts, like After Effects: every installed TTF/OTF (system + per-user) by its real family/style/weight.
// Only the sfnt table directory, 'name' and 'OS/2' are read (not whole files); the list is built once and cached.
const FONT_DIRS = [path.join(process.env.WINDIR || 'C:\\Windows', 'Fonts'),
  process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Microsoft', 'Windows', 'Fonts')].filter(Boolean);
let fontList = null;
function readAt(fd, pos, len) { const b = Buffer.alloc(len); fs.readSync(fd, b, 0, len, pos); return b; }
function fontInfo(file) {
  const fd = fs.openSync(file, 'r');
  try {
    const hd = readAt(fd, 0, 12), n = hd.readUInt16BE(4), dir = readAt(fd, 12, n * 16);
    const tab = {};
    for (let i = 0; i < n; i++) tab[dir.toString('latin1', i * 16, i * 16 + 4)] = [dir.readUInt32BE(i * 16 + 8), dir.readUInt32BE(i * 16 + 12)];
    if (!tab.name) return null;
    const nt = readAt(fd, tab.name[0], tab.name[1]), count = nt.readUInt16BE(2), so = nt.readUInt16BE(4), names = {};
    for (let i = 0; i < count; i++) {
      const r = 6 + i * 12, pid = nt.readUInt16BE(r), lang = nt.readUInt16BE(r + 4), id = nt.readUInt16BE(r + 6);
      const len = nt.readUInt16BE(r + 8), off = so + nt.readUInt16BE(r + 10);
      if (![1, 2, 4, 16, 17].includes(id) || off + len > nt.length) continue;
      let s = null;
      if (pid === 3 || pid === 0) { const u = Buffer.from(nt.subarray(off, off + len)); u.swap16(); s = u.toString('utf16le'); }
      else if (pid === 1 && lang === 0) s = nt.toString('latin1', off, off + len);
      if (s && (names[id] === undefined || (pid === 3 && lang === 0x409))) names[id] = s;
    }
    let weight = 400, italic = false;
    if (tab['OS/2']) { const o = readAt(fd, tab['OS/2'][0], 64); weight = o.readUInt16BE(4) || 400; italic = !!(o.readUInt16BE(62) & 1); }
    const family = names[16] || names[1], style = names[17] || names[2] || 'Regular';
    if (!family || family.startsWith('.')) return null;
    if (/italic|oblique/i.test(style)) italic = true;
    // the style name beats OS/2 usWeightClass (Windows fonts often store 250/300 for Thin/ExtraLight)
    const byName = [[/extra\s*black|ultra\s*black/i, 950], [/black|heavy/i, 900], [/extra\s*bold|ultra\s*bold/i, 800],
      [/semi\s*bold|demi\s*bold/i, 600], [/bold/i, 700], [/medium/i, 500], [/extra\s*light|ultra\s*light/i, 200],
      [/thin|hairline/i, 100], [/light/i, 300], [/regular|book|normal|roman/i, 400]].find(([re]) => re.test(style));
    if (byName) weight = Math.min(900, byName[1]);
    return { family, style, weight: Math.max(100, Math.min(900, Math.round(weight / 100) * 100)), italic };
  } finally { fs.closeSync(fd); }
}
function fonts() {
  if (fontList) return fontList;
  const seen = new Set(); fontList = [];
  for (const d of FONT_DIRS) {
    let files = []; try { files = fs.readdirSync(d); } catch (e) { continue; }
    for (const f of files) {
      if (!/\.(ttf|otf)$/i.test(f)) continue;
      try {
        const info = fontInfo(path.join(d, f)); if (!info) continue;
        const key = `${info.family}|${info.style}`.toLowerCase(); if (seen.has(key)) continue; seen.add(key);
        fontList.push({ id: fontList.length, ...info, file: path.join(d, f) });
      } catch (e) { /* unreadable font: skip */ }
    }
  }
  fontList.sort((a, b) => a.family.localeCompare(b.family) || a.weight - b.weight || a.italic - b.italic);
  fontList.forEach((x, i) => x.id = i);
  return fontList;
}
// the installed file closest to a layer style (family, weight, italic) - what render.py draws subtitles with
function fontFile(st) {
  const want = st.bold ? 900 : (st.weight || 400), it = !!st.italic;
  const faces = fonts().filter(f => f.family.toLowerCase() === String(st.font || '').toLowerCase());
  const pick = (faces.length ? faces : fonts().filter(f => f.family === 'Arial'))
    .sort((a, b) => (a.italic !== it) - (b.italic !== it) || Math.abs(a.weight - want) - Math.abs(b.weight - want))[0];
  return pick ? pick.file : path.join(process.env.WINDIR || 'C:\\Windows', 'Fonts', 'arial.ttf');
}
function serveFont(id, res) {
  const f = fonts()[+id];
  if (!f) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { 'Content-Type': /\.otf$/i.test(f.file) ? 'font/otf' : 'font/ttf', 'Cache-Control': 'max-age=86400' });
  fs.createReadStream(f.file).pipe(res);
}

function upload(req, res, url) {
  fs.mkdirSync(UPLOADS, { recursive: true });
  const name = safe(url.searchParams.get('name'));
  let file = name, k = 2; while (fs.existsSync(path.join(UPLOADS, file))) file = name.replace(/(\.\w+)?$/, `_${k++}$1`);
  const out = fs.createWriteStream(path.join(UPLOADS, file));
  req.pipe(out); out.on('finish', () => send(res, 200, { src: `uploads/${file}` })); out.on('error', e => send(res, 500, { error: e.message }));
}

http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x');
  if (url.pathname === '/api/render' && req.method === 'POST') return startRender(req, res);
  if (url.pathname.startsWith('/api/render/') && req.method === 'GET') return renderStatus(url.pathname.split('/').pop(), res);
  if (url.pathname === '/api/upload' && req.method === 'POST') return upload(req, res, url);
  if (url.pathname === '/api/drive') return driveCheck(url, res);
  if (url.pathname === '/api/fonts') return send(res, 200, fonts().map(({ file, ...x }) => x));
  if (url.pathname.startsWith('/api/font/')) return serveFont(url.pathname.split('/').pop(), res);
  let p = decodeURIComponent(url.pathname);
  if (p.endsWith('/')) p += 'index.html';
  const f = path.join(ROOT, p);
  if (!f.startsWith(ROOT)) { res.writeHead(403); return res.end(); }
  fs.stat(f, (err, st) => {
    if (err || !st.isFile()) { res.writeHead(404); return res.end('not found'); }
    const type = TYPES[path.extname(f).toLowerCase()] || 'application/octet-stream';
    const head = { 'Content-Type': type, 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store' };
    if (url.searchParams.has('download')) head['Content-Disposition'] = `attachment; filename="${path.basename(f)}"`;
    const m = /bytes=(\d*)-(\d*)/.exec(req.headers.range || '');
    if (m) {
      const a = m[1] ? +m[1] : st.size - +m[2];
      // open-ended ranges ("bytes=N-", what <video>/<audio> ask for) are answered in 4 MB pieces: a media element that
      // has buffered enough keeps its response paused, and with a dozen clips the browser's 6 connections per host
      // were all held that way - the clip at the playhead could not load (black preview)
      let b = m[1] && m[2] ? Math.min(+m[2], st.size - 1) : st.size - 1;
      if (m[1] && !m[2]) b = Math.min(b, a + (4 << 20) - 1);
      if (a > b || a >= st.size) { res.writeHead(416, { 'Content-Range': `bytes */${st.size}` }); return res.end(); }
      res.writeHead(206, { ...head, 'Content-Range': `bytes ${a}-${b}/${st.size}`, 'Content-Length': b - a + 1 });
      return fs.createReadStream(f, { start: a, end: b }).pipe(res);
    }
    res.writeHead(200, { ...head, 'Content-Length': st.size });
    fs.createReadStream(f).pipe(res);
  });
}).listen(PORT, () => console.log(`editor on http://localhost:${PORT}  (Export MP4 renders locally into editor/exports)`));
