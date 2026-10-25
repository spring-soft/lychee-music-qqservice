/* QQ 音乐网页播放器 —— 只调 /rest/*（Subsonic 协议），音频由浏览器直接连 QQ CDN。
 *
 * 认证：登录后服务端下发 HttpOnly cookie，/rest 直接认它 —— 所以这里前端
 * 从不接触密码、也不拼 u/t/s。第三方客户端仍走标准 Subsonic 认证。
 */
'use strict';

const $ = (s) => document.querySelector(s);
const state = {
  queue: [],        // 当前播放队列（playlist entries 或搜索结果）
  index: -1,
  tab: 'playlists',
  playlistId: null,
  lyrics: [],       // [{start, value}]
  lyricsTimer: 0,
};

/* ---------------------------------------------------------------- 工具 */

function toast(msg, ms = 2600) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add('hidden'), ms);
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
}

async function api(name, params) {
  const qs = new URLSearchParams(params || {}).toString();
  const r = await fetch('/rest/' + name + '.view?' + qs, { credentials: 'same-origin' });
  let j;
  try { j = await r.json(); } catch (e) { throw new Error('服务器返回不是 JSON（' + r.status + '）'); }
  const sr = j['subsonic-response'] || {};
  if (sr.status !== 'ok') {
    const err = sr.error || {};
    if (err.code === 40) showLogin();
    throw new Error(err.message || ('请求失败 code=' + err.code));
  }
  return sr;
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
    await loadPlaylists();
  } catch (e) {
    showLogin();
  }
}

$('#login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = $('#login-btn');
  btn.disabled = true;
  btn.textContent = '登录中…';
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
    btn.disabled = false;
    btn.textContent = '登录';
  }
});

$('#btn-logout').addEventListener('click', async () => {
  try { await fetch('/logout', { method: 'POST' }); } catch (e) {}
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
  const sr = await api('getPlaylist', { id });
  const entries = (sr.playlist && sr.playlist.entry) || [];
  state.playlistId = id;
  state.queue = entries;
  $('#songs-title').textContent = name + '（' + entries.length + ' 首）';
  renderList($('#songs'), entries);
  switchTab('songs', true);
  // 同步把当前播放行标出来
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
    row.addEventListener('click', () => playAt(i));
    box.appendChild(row);
  });
}

function markPlaying() {
  const box = $('#songs');
  if (!box) return;
  box.querySelectorAll('.row').forEach((r) => {
    const isMe = Number(r.dataset.idx) === state.index && !$('#view-songs').classList.contains('hidden');
    r.classList.toggle('playing', isMe);
    const idx = r.querySelector('.r-idx');
    if (idx) idx.textContent = isMe ? '♪' : String(Number(r.dataset.idx) + 1);
  });
}

/* ---------------------------------------------------------------- 搜索 */

$('#search-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const q = $('#search-input').value.trim();
  if (!q) return;
  try {
    const sr = await api('search3', { query: q, songCount: 50 });
    const songs = (sr.searchResult3 && sr.searchResult3.song) || [];
    state.queue = songs;
    renderList($('#search-results'), songs);
    if (!songs.length) toast('没找到');
  } catch (e) { toast('搜索失败：' + e.message); }
});

/* ---------------------------------------------------------------- 播放 */

const au = $('#au');

function coverUrl(song, size) {
  if (!song || !song.coverArt || song.coverArt === 'none') return null;
  return '/rest/getCoverArt.view?size=' + (size || 300) + '&id=' + encodeURIComponent(song.coverArt);
}

function playAt(i) {
  const q = state.queue;
  if (i < 0 || i >= q.length) return;
  state.index = i;
  const s = q[i];
  au.src = '/rest/stream.view?id=' + encodeURIComponent(s.id);
  au.play().catch((e) => toast('播放失败：' + e.message));
  $('#btn-play').textContent = '⏸';
  $('#np-title').textContent = s.title || '';
  $('#np-artist').textContent = (s.artist || '') + (s.album ? ' · ' + s.album : '');
  const c = coverUrl(s);
  $('#np-cover').src = c || 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
  markPlaying();
  loadLyrics(s);
  try {
    if ('mediaSession' in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: s.title || '', artist: s.artist || '', album: s.album || '',
        artwork: c ? [{ src: c, sizes: '300x300', type: 'image/jpeg' }] : [],
      });
    }
  } catch (e) {}
}

$('#btn-play').addEventListener('click', () => {
  if (!au.src) { if (state.queue.length) playAt(0); return; }
  if (au.paused) au.play().catch((e) => toast(e.message));
  else au.pause();
});
$('#btn-next').addEventListener('click', () => { if (state.queue.length) playAt((state.index + 1) % state.queue.length); });
$('#btn-prev').addEventListener('click', () => {
  if (state.queue.length) playAt((state.index - 1 + state.queue.length) % state.queue.length);
});

au.addEventListener('play', () => { $('#btn-play').textContent = '⏸'; });
au.addEventListener('pause', () => { $('#btn-play').textContent = '▶'; });
au.addEventListener('timeupdate', () => {
  const d = au.duration || 0;
  if (d > 0) {
    $('#seek').value = String(Math.round((au.currentTime / d) * 1000));
    $('#t-dur').textContent = fmtTime(d);
  }
  $('#t-cur').textContent = fmtTime(au.currentTime);
  syncLyrics(au.currentTime);
});
au.addEventListener('ended', () => { if (state.queue.length) playAt((state.index + 1) % state.queue.length); });
au.addEventListener('error', () => {
  if (au.src) toast('这首播放失败（可能是付费/版权限制），换一首试试');
});

$('#seek').addEventListener('input', () => {
  const d = au.duration;
  if (!d || !isFinite(d)) return;
  au.currentTime = (Number($('#seek').value) / 1000) * d;
});

/* ---------------------------------------------------------------- 歌词 */

async function loadLyrics(song) {
  state.lyrics = [];
  $('#lyrics-body').innerHTML = '<div class="empty">加载歌词…</div>';
  try {
    const sr = await api('getLyricsBySongId', { id: song.id });
    const sl = (sr.lyricsList && sr.lyricsList.structuredLyrics) || [];
    const lines = (sl[0] && sl[0].line) || [];
    state.lyrics = lines;
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
}

function syncLyrics(t) {
  if (!state.lyrics.length || $('#lyrics').classList.contains('hidden')) return;
  const ms = t * 1000;
  let idx = -1;
  for (let i = 0; i < state.lyrics.length; i++) {
    if ((state.lyrics[i].start || 0) <= ms + 120) idx = i; else break;
  }
  const box = $('#lyrics-body');
  box.querySelectorAll('.l.on').forEach((e) => e.classList.remove('on'));
  if (idx >= 0) {
    const el = box.querySelector('.l[data-i="' + idx + '"]');
    if (el) {
      el.classList.add('on');
      if (!syncLyrics._last || syncLyrics._last !== idx) {
        syncLyrics._last = idx;
        const top = el.offsetTop - box.clientHeight / 2 + el.clientHeight / 2;
        box.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
      }
    }
  }
}

$('#btn-lyrics').addEventListener('click', () => $('#lyrics').classList.toggle('hidden'));
$('#lyrics-close').addEventListener('click', () => $('#lyrics').classList.add('hidden'));

/* ---------------------------------------------------------------- 视图切换 */

function switchTab(name, force) {
  if (!force && state.tab === name) return;
  state.tab = name;
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
  $('#view-playlists').classList.toggle('hidden', name !== 'playlists');
  $('#view-songs').classList.toggle('hidden', name !== 'songs');
  $('#view-search').classList.toggle('hidden', name !== 'search');
}

function $$(s) { return Array.from(document.querySelectorAll(s)); }

$$('.tab').forEach((t) => t.addEventListener('click', () => {
  if (t.dataset.tab === 'search') switchTab('search');
  else switchTab('playlists');
}));
$('#btn-back').addEventListener('click', () => switchTab('playlists'));

/* ---------------------------------------------------------------- 启动 */

boot();
