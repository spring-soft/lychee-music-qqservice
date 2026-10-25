/* QQ 音乐网页播放器 —— 只调 /rest/*（Subsonic），音频由浏览器直连 QQ CDN。
 *
 * 认证：登录后服务端下发 HttpOnly cookie，/rest 直接认它 —— 前端从不接触密码。
 * 数据：歌单/搜索/队列/歌词都来自 /rest；音频是 302 到 QQ CDN 的 https 直链。
 */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const LS_SESSION = 'qm.session';
const LS_VOL = 'qm.vol';

const state = {
  queue: [],
  index: -1,
  name: '',          // 当前歌单/搜索结果名
  tab: 'playlists',
  lyrics: [],
  pendingSeek: 0,    // 恢复上次播放时用
  starId: '',
};

/* ---------------------------------------------------------------- 工具 */

let toastTimer = 0;
function toast(msg, ms = 2600) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), ms);
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
}

async function api(name, params) {
  const qs = new URLSearchParams(params || {}).toString();
  let r;
  try {
    r = await fetch('/rest/' + name + '.view?' + qs, { credentials: 'same-origin' });
  } catch (e) {
    throw new Error('网络错误（连不上服务器）');
  }
  let j;
  try { j = await r.json(); } catch (e) { throw new Error('服务器返回异常（' + r.status + '）'); }
  const sr = j['subsonic-response'] || {};
  if (sr.status !== 'ok') {
    const err = sr.error || {};
    if (err.code === 40) showLogin();
    if (err.code === 50) throw new Error(err.message || '请求太频繁');
    throw new Error(err.message || ('请求失败 code=' + err.code));
  }
  return sr;
}

function coverUrl(song, size) {
  if (!song || !song.coverArt || song.coverArt === 'none') return null;
  return '/rest/getCoverArt.view?size=' + (size || 300) + '&id=' + encodeURIComponent(song.coverArt);
}

/* ---------------------------------------------------------------- 登录 */

function showLogin() {
  $('#app').classList.add('hidden');
  $('#login').classList.remove('hidden');
}

async function boot() {
  try {
    const me = await (await fetch('/api/me', { credentials: 'same-origin' })).json();
    if (!me.ok) { showLogin(); return; }
    $('#login').classList.add('hidden');
    $('#app').classList.remove('hidden');
    restoreSession();
    await loadPlaylists();
  } catch (e) {
    showLogin();
  }
}

$('#login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = $('#login-btn');
  btn.disabled = true; btn.textContent = '登录中…';
  $('#login-err').textContent = '';
  try {
    const fd = new URLSearchParams();
    fd.set('user', $('#login-user').value.trim());
    fd.set('password', $('#login-pass').value);
    const r = await fetch('/login', {
      method: 'POST', body: fd, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) { $('#login-err').textContent = j.error || '登录失败'; return; }
    $('#login-pass').value = '';
    await boot();
  } catch (e) {
    $('#login-err').textContent = '网络错误：' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = '登录';
  }
});

$('#btn-logout').addEventListener('click', async () => {
  try { await fetch('/logout', { method: 'POST' }); } catch (e) {}
  try { localStorage.removeItem(LS_SESSION); } catch (e) {}
  location.reload();
});

/* ---------------------------------------------------------------- 歌单 */

async function loadPlaylists() {
  const sr = await api('getPlaylists');
  const list = (sr.playlists && sr.playlists.playlist) || [];
  const box = $('#playlists');
  box.innerHTML = '';
  if (!list.length) { box.innerHTML = '<div class="empty">没有歌单</div>'; return; }
  for (const p of list) {
    const b = document.createElement('button');
    b.className = 'card';
    b.innerHTML = '<div class="c-name"></div><div class="c-desc"></div>';
    b.querySelector('.c-name').textContent = p.name || p.id;
    b.querySelector('.c-desc').textContent = p.songCount ? (p.songCount + ' 首') : '点开看看';
    b.addEventListener('click', () => openPlaylist(p.id, p.name || p.id).catch(e => toast(e.message)));
    box.appendChild(b);
  }
}

async function openPlaylist(id, name) {
  const box = $('#songs');
  box.innerHTML = '<div class="loading-row">加载歌单…（收藏较多时要翻几页）</div>';
  switchTab('songs', true);
  $('#songs-title').textContent = name + '（加载中…）';
  let entries = [];
  try {
    const sr = await api('getPlaylist', { id });
    entries = (sr.playlist && sr.playlist.entry) || [];
  } catch (e) {
    box.innerHTML = '';
    $('#songs-title').textContent = name;
    toast('加载歌单失败：' + e.message);
    return;
  }
  state.queue = entries;
  state.index = -1;
  state.name = name;
  $('#songs-title').textContent = name + '（' + entries.length + ' 首）';
  renderList(box, entries);
  markPlaying();
}

/* ---------------------------------------------------------------- 列表 */

function renderList(box, songs) {
  box.innerHTML = '';
  if (!songs || !songs.length) { box.innerHTML = '<div class="empty">空</div>'; return; }
  songs.forEach((s, i) => {
    const row = document.createElement('button');
    row.className = 'row';
    row.dataset.idx = String(i);
    row.innerHTML =
      '<span class="r-idx"></span>' +
      '<span class="r-main"><div class="r-title"></div><div class="r-artist"></div></span>' +
      '<span class="r-meta"><span class="badge"></span><span class="r-dur"></span></span>';
    row.querySelector('.r-idx').textContent = String(i + 1);
    row.querySelector('.r-title').textContent = s.title || '(无题)';
    row.querySelector('.r-artist').textContent = s.artist || '';
    row.querySelector('.badge').textContent = (s.suffix || '').toUpperCase();
    row.querySelector('.r-dur').textContent = fmtTime(s.duration);
    row.addEventListener('click', () => {
      if (state.index === i && !au.paused) { au.pause(); return; }
      playAt(i);
    });
    box.appendChild(row);
  });
}

function markPlaying() {
  ['#songs', '#search-results', '#queue-list'].forEach((sel) => {
    const box = $(sel);
    if (!box) return;
    box.querySelectorAll('.row').forEach((r) => {
      const isMe = Number(r.dataset.idx) === state.index;
      r.classList.toggle('playing', isMe);
      const idx = r.querySelector('.r-idx');
      if (idx && sel !== '#queue-list') idx.textContent = isMe ? '♪' : String(Number(r.dataset.idx) + 1);
    });
  });
}

/* ---------------------------------------------------------------- 搜索 */

$('#search-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const q = $('#search-input').value.trim();
  if (!q) return;
  const box = $('#search-results');
  box.innerHTML = '<div class="loading-row">搜索中…</div>';
  try {
    const sr = await api('search3', { query: q, songCount: 50 });
    const songs = (sr.searchResult3 && sr.searchResult3.song) || [];
    state.queue = songs;
    state.index = -1;
    state.name = '搜索：' + q;
    renderList(box, songs);
    if (!songs.length) box.innerHTML = '<div class="empty">没找到</div>';
  } catch (e) { box.innerHTML = ''; toast('搜索失败：' + e.message); }
});

/* ---------------------------------------------------------------- 播放 */

const au = $('#au');

function updateNowPlaying(s) {
  $('#np-title').textContent = (s && s.title) || '未在播放';
  $('#np-artist').textContent = s ? ((s.artist || '') + (s.album ? ' · ' + s.album : '')) : '';
  const c = coverUrl(s);
  $('#np-cover').src = c || 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
  state.starId = (s && s.id) || '';
  $('#btn-star').classList.remove('on');
  try {
    if ('mediaSession' in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: (s && s.title) || '', artist: (s && s.artist) || '',
        album: (s && s.album) || '',
        artwork: c ? [{ src: c, sizes: '300x300', type: 'image/jpeg' }] : [],
      });
    }
  } catch (e) {}
}

function ensureSrc(s, autoplay) {
  if (au.dataset.songId !== s.id) {
    au.dataset.songId = s.id;
    au.src = '/rest/stream.view?id=' + encodeURIComponent(s.id);
  }
  if (autoplay) {
    au.play().catch((e) => toast('播放失败：' + e.message));
  }
}

function playAt(i) {
  const q = state.queue;
  if (i < 0 || i >= q.length) return;
  state.index = i;
  const s = q[i];
  state.pendingSeek = 0;
  ensureSrc(s, true);
  updateNowPlaying(s);
  $('#btn-play').textContent = '⏸';
  markPlaying();
  loadLyrics(s);
  saveSession();
}

function togglePlay() {
  if (!au.src) {
    if (!state.queue.length) { toast('先选一个歌单或搜索'); return; }
    if (state.index < 0) { playAt(0); return; }
    // 从上次会话恢复：不能丢掉 pendingSeek（否则“继续播放”会从头开始）
    const s = state.queue[state.index];
    ensureSrc(s, true);
    $('#btn-play').textContent = '⏸';
    markPlaying();
    loadLyrics(s);
    return;
  }
  if (au.paused) au.play().catch((e) => toast(e.message));
  else au.pause();
}

function nextSong() {
  if (!state.queue.length) return;
  playAt((state.index + 1) % state.queue.length);
}

function prevSong() {
  if (!state.queue.length) return;
  if (au.currentTime > 3) { au.currentTime = 0; return; }
  playAt((state.index - 1 + state.queue.length) % state.queue.length);
}

$('#btn-play').addEventListener('click', togglePlay);
$('#btn-next').addEventListener('click', nextSong);
$('#btn-prev').addEventListener('click', prevSong);

$('#btn-star').addEventListener('click', async () => {
  if (!state.starId) { toast('先播一首歌'); return; }
  try {
    await api('star', { id: state.starId });
    $('#btn-star').classList.add('on');
    toast('已加入 QQ「我喜欢」（取消收藏要去 QQ 音乐 App 里操作）');
  } catch (e) { toast('加星失败：' + e.message); }
});

/* 随机：把当前队列打乱，当前歌放最前 */
$('#btn-shuffle').addEventListener('click', () => {
  if (!state.queue.length) { toast('先打开一个歌单或搜索'); return; }
  const cur = state.index >= 0 ? state.queue[state.index] : null;
  const rest = state.queue.filter((x) => !cur || x.id !== cur.id);
  for (let i = rest.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [rest[i], rest[j]] = [rest[j], rest[i]];
  }
  state.queue = cur ? [cur, ...rest] : rest;
  state.index = cur ? 0 : -1;
  if (state.tab === 'songs') renderList($('#songs'), state.queue);
  else renderList($('#search-results'), state.queue);
  markPlaying();
  toast('已随机排序');
});

/* ---------------------------------------------------------------- 音频事件 */

au.addEventListener('play', () => { $('#btn-play').textContent = '⏸'; });
au.addEventListener('pause', () => { $('#btn-play').textContent = '▶'; });
au.addEventListener('waiting', () => $('.player').classList.add('loading'));
au.addEventListener('playing', () => $('.player').classList.remove('loading'));
au.addEventListener('canplay', () => {
  $('.player').classList.remove('loading');
  if (state.pendingSeek > 0 && au.duration) {
    au.currentTime = Math.min(state.pendingSeek, Math.max(0, au.duration - 2));
    state.pendingSeek = 0;
  }
});
au.addEventListener('loadedmetadata', () => { $('#t-dur').textContent = fmtTime(au.duration); });
au.addEventListener('timeupdate', () => {
  const d = au.duration || 0;
  if (d > 0) {
    $('#seek').value = String(Math.round((au.currentTime / d) * 1000));
    $('#t-dur').textContent = fmtTime(d);
  }
  $('#t-cur').textContent = fmtTime(au.currentTime);
  syncLyrics(au.currentTime);
  saveSessionThrottled();
});
au.addEventListener('ended', nextSong);
au.addEventListener('error', () => {
  $('.player').classList.remove('loading');
  if (au.src) toast('这首播放失败（可能是付费/版权限制），换一首试试');
});

$('#seek').addEventListener('input', () => {
  const d = au.duration;
  if (!d || !isFinite(d)) return;
  au.currentTime = (Number($('#seek').value) / 1000) * d;
});

/* ---------------------------------------------------------------- 队列抽屉 */

function renderQueue() {
  const box = $('#queue-list');
  box.innerHTML = '';
  if (!state.queue.length) { box.innerHTML = '<div class="empty">队列是空的</div>'; return; }
  state.queue.forEach((s, i) => {
    const row = document.createElement('button');
    row.className = 'row';
    row.dataset.idx = String(i);
    row.innerHTML = '<span class="r-idx"></span><span class="r-main">' +
      '<div class="r-title"></div><div class="r-artist"></div></span>' +
      '<span class="r-meta"><span class="r-dur"></span></span>';
    row.querySelector('.r-idx').textContent = String(i + 1);
    row.querySelector('.r-title').textContent = s.title || '';
    row.querySelector('.r-artist').textContent = s.artist || '';
    row.querySelector('.r-dur').textContent = fmtTime(s.duration);
    row.addEventListener('click', () => { playAt(i); });
    box.appendChild(row);
  });
  markPlaying();
  $('#queue-title').textContent = '播放队列（' + state.queue.length + ' 首）';
}

$('#btn-queue').addEventListener('click', () => {
  const q = $('#queue');
  const show = q.classList.contains('hidden');
  q.classList.toggle('hidden', !show);
  if (show) renderQueue();
});
$('#queue-close').addEventListener('click', () => $('#queue').classList.add('hidden'));

/* 音量（浏览器本地音量，不经过服务器） */
const vol = $('#vol');
try {
  const v = Number(localStorage.getItem(LS_VOL));
  if (v >= 0 && v <= 100) { vol.value = String(v); au.volume = v / 100; }
} catch (e) {}
vol.addEventListener('input', () => {
  const v = Number(vol.value);
  au.volume = v / 100;
  $('#vol-icon').textContent = v === 0 ? '🔇' : (v < 50 ? '🔉' : '🔊');
  try { localStorage.setItem(LS_VOL, String(v)); } catch (e) {}
});

/* ---------------------------------------------------------------- 歌词 */

async function loadLyrics(song) {
  state.lyrics = [];
  $('#lyrics-body').innerHTML = '<div class="empty">加载歌词…</div>';
  try {
    const sr = await api('getLyricsBySongId', { id: song.id });
    const sl = (sr.lyricsList && sr.lyricsList.structuredLyrics) || [];
    state.lyrics = (sl[0] && sl[0].line) || [];
    renderLyrics();
  } catch (e) {
    $('#lyrics-body').innerHTML = '<div class="empty">没有歌词</div>';
  }
}

function renderLyrics() {
  const box = $('#lyrics-body');
  if (!state.lyrics.length) { box.innerHTML = '<div class="empty">没有歌词</div>'; return; }
  box.innerHTML = '';
  state.lyrics.forEach((ln, i) => {
    const d = document.createElement('div');
    d.className = 'l';
    d.dataset.i = String(i);
    d.textContent = ln.value || '';
    box.appendChild(d);
  });
  box.scrollTop = 0;
}

let lastLyricIdx = -1;
function syncLyrics(t) {
  if (!state.lyrics.length || $('#lyrics').classList.contains('hidden')) return;
  const ms = t * 1000;
  let idx = -1;
  for (let i = 0; i < state.lyrics.length; i++) {
    if ((state.lyrics[i].start || 0) <= ms + 120) idx = i; else break;
  }
  if (idx === lastLyricIdx) return;
  lastLyricIdx = idx;
  const box = $('#lyrics-body');
  box.querySelectorAll('.l.on').forEach((e) => e.classList.remove('on'));
  if (idx >= 0) {
    const el = box.querySelector('.l[data-i="' + idx + '"]');
    if (el) {
      el.classList.add('on');
      const top = el.offsetTop - box.clientHeight / 2 + el.clientHeight / 2;
      box.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
    }
  }
}

$('#btn-lyrics').addEventListener('click', () => {
  const ly = $('#lyrics');
  const show = ly.classList.contains('hidden');
  if (show) $('#queue').classList.add('hidden');
  ly.classList.toggle('hidden', !show);
  if (show) { lastLyricIdx = -1; syncLyrics(au.currentTime); }
});
$('#lyrics-close').addEventListener('click', () => $('#lyrics').classList.add('hidden'));

/* ---------------------------------------------------------------- 会话恢复 */

let saveTimer = 0;
function saveSession() {
  try {
    if (!state.queue.length) return;
    localStorage.setItem(LS_SESSION, JSON.stringify({
      queue: state.queue.slice(0, 500),
      index: state.index,
      name: state.name,
      pos: au.currentTime || 0,
    }));
  } catch (e) {}
}
function saveSessionThrottled() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveSession, 4000);
}

function restoreSession() {
  let s;
  try { s = JSON.parse(localStorage.getItem(LS_SESSION) || 'null'); } catch (e) { return; }
  if (!s || !Array.isArray(s.queue) || !s.queue.length) return;
  state.queue = s.queue;
  state.index = (typeof s.index === 'number' && s.index >= 0 && s.index < s.queue.length) ? s.index : 0;
  state.name = s.name || '';
  state.pendingSeek = Number(s.pos) || 0;
  const cur = state.queue[state.index];
  updateNowPlaying(cur);
  $('#btn-play').textContent = '▶';
  if (state.pendingSeek > 3) {
    $('#np-artist').textContent += ' · 上次播放到 ' + fmtTime(state.pendingSeek);
  }
}

/* ---------------------------------------------------------------- 视图切换 */

function switchTab(name, force) {
  if (!force && state.tab === name) return;
  state.tab = name;
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  $('#view-playlists').classList.toggle('hidden', name !== 'playlists');
  $('#view-songs').classList.toggle('hidden', name !== 'songs');
  $('#view-search').classList.toggle('hidden', name !== 'search');
}

$$('.tab').forEach((t) => t.addEventListener('click', () => {
  if (t.dataset.tab === 'search') switchTab('search');
  else switchTab('playlists');
}));
$('#btn-back').addEventListener('click', () => switchTab('playlists'));
$('#btn-refresh').addEventListener('click', () => {
  loadPlaylists().then(() => toast('已刷新')).catch((e) => toast(e.message));
});

/* ---------------------------------------------------------------- 键盘 */

document.addEventListener('keydown', (ev) => {
  const tag = (ev.target && ev.target.tagName) || '';
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  if (ev.code === 'Space') { ev.preventDefault(); togglePlay(); }
  else if (ev.key === 'ArrowRight') { ev.preventDefault(); au.currentTime = Math.min((au.duration || 0), au.currentTime + 5); }
  else if (ev.key === 'ArrowLeft') { ev.preventDefault(); au.currentTime = Math.max(0, au.currentTime - 5); }
  else if (ev.key === 'ArrowUp') { ev.preventDefault(); vol.value = String(Math.min(100, Number(vol.value) + 5)); vol.dispatchEvent(new Event('input')); }
  else if (ev.key === 'ArrowDown') { ev.preventDefault(); vol.value = String(Math.max(0, Number(vol.value) - 5)); vol.dispatchEvent(new Event('input')); }
  else if (ev.key === 'n' || ev.key === 'N') nextSong();
  else if (ev.key === 'p' || ev.key === 'P') prevSong();
});

/* ---------------------------------------------------------------- 媒体键 */

try {
  if ('mediaSession' in navigator) {
    navigator.mediaSession.setActionHandler('play', () => au.play().catch(() => {}));
    navigator.mediaSession.setActionHandler('pause', () => au.pause());
    navigator.mediaSession.setActionHandler('nexttrack', nextSong);
    navigator.mediaSession.setActionHandler('previoustrack', prevSong);
  }
} catch (e) {}

window.addEventListener('beforeunload', saveSession);

/* ---------------------------------------------------------------- 启动 */

boot();
