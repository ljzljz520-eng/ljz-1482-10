/* 民宿房源视频编辑台 - single-file vanilla JS client.
 * State model: every project save carries base_version; 409 is surfaced
 * with the other editor's new version instead of overwriting. */
'use strict';

const state = {
  userId: null, users: [], propertyId: null, projectId: null,
  timeline: null, baseVersion: 0, assets: [], rooms: [], channels: [],
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function toast(msg, isErr = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (isErr ? ' err' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add('hidden'), 4200);
}

async function api(method, url, body) {
  const opts = { method, headers: { 'X-User-Id': state.userId } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch { data = {}; }
  if (!res.ok) {
    const err = new Error(data.detail || data.error || `HTTP ${res.status}`);
    err.payload = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

// ------------------------------------------------------------- bootstrap

async function boot() {
  state.users = (await api('GET', '/api/users')).users;
  state.channels = (await api('GET', '/api/channels')).channels;
  state.userId = String(localStorage.getItem('wb_user') || state.users[0]?.id || '');
  renderUserSelect();
  await show('home');
  document.addEventListener('click', e => {
    const a = e.target.closest('nav a');
    if (a) { e.preventDefault(); show(a.dataset.view); }
  });
}

function renderUserSelect() {
  $('#userSelect').innerHTML = state.users.map(u =>
    `<option value="${u.id}" ${String(u.id) === state.userId ? 'selected' : ''}>${esc(u.display_name)}</option>`
  ).join('');
  $('#userSelect').onchange = async e => {
    state.userId = e.target.value;
    localStorage.setItem('wb_user', state.userId);
    toast('已切换用户');
    await show('home');
  };
}

function mountTpl(id) {
  const tpl = $('#tpl-' + id);
  $('#main').innerHTML = '';
  $('#main').appendChild(tpl.content.cloneNode(true));
}

async function ensureProperty() {
  if (state.propertyId) return;
  const list = await api('GET', '/api/home');
  const p = list.projects[0];
  state.propertyId = p ? p.property_id : null;
}

async function show(view) {
  $$('nav a').forEach(a => a.classList.toggle('active', a.dataset.view === view));
  if (view === 'home') return viewHome();
  await ensureProperty();
  if (view === 'property') return viewProperty();
  if (view === 'assets') return viewAssets();
  if (view === 'project') return viewProject();
}

// ------------------------------------------------------------------ home

async function viewHome() {
  mountTpl('home');
  $('#today').textContent = await (async () => (await api('GET', '/health')).date)();
  const data = await api('GET', '/api/home');
  const tb = $('#homeTable tbody');
  if (!data.projects.length) {
    tb.innerHTML = '<tr><td colspan="4">暂无项目（种子脚本未运行？执行 python -m scripts.seed）</td></tr>';
  }
  tb.innerHTML = data.projects.map(p => `
    <tr>
      <td>${esc(p.title)}</td>
      <td>${esc(p.role)}</td>
      <td><span class="badge ok">游标 v${p.cursor_version ?? '?'}</span></td>
      <td>
        <button class="tiny" data-open="${p.project_id}">打开（游标版本）</button>
        <button class="tiny" data-resume="${p.project_id}">恢复上次项目（重查权限）</button>
      </td>
    </tr>`).join('');
  tb.onclick = async e => {
    const openId = e.target.dataset.open;
    const resumeId = e.target.dataset.resume;
    try {
      if (openId) { state.projectId = +openId; await show('project'); }
      if (resumeId) {
        const r = await api('POST', `/api/projects/${resumeId}/resume`);
        state.projectId = +resumeId;
        toast(`已恢复到 v${r.resumed_version}`);
        await show('project');
      }
    } catch (err) {
      if (err.status === 403) toast('权限已变更：' + err.message, true);
      else toast(err.message, true);
    }
  };
  $('#newProjectBtn').onclick = async () => {
    try {
      const name = prompt('新项目标题', '新房型短视频');
      if (!name) return;
      const r = await api('POST', '/api/projects', {
        property_id: state.propertyId, title: name,
        channels: ['landscape_169', 'douyin'] });
      state.projectId = r.project_id;
      await show('project');
    } catch (err) { toast(err.message, true); }
  };
}

// -------------------------------------------------------------- property

async function loadRooms() {
  const p = await api('GET', `/api/properties/${state.propertyId}`);
  state.rooms = p.room_types;
  return p;
}

async function viewProperty() {
  mountTpl('property');
  const p = await loadRooms();
  const root = $('#roomList');
  root.innerHTML = `<p>房源：<strong>${esc(p.name)}</strong> ${esc(p.address)}</p>` +
    state.rooms.map(r => `
      <div class="room" data-room="${r.id}">
        <h2>${esc(r.name)} <button class="tiny" data-rev="${r.id}">新增规则修订</button>
          <button class="tiny" data-feat="${r.id}">登记设施/窗景</button></h2>
        <div class="roomBody"></div>
      </div>`).join('');
  for (const r of state.rooms) {
    const detail = await api('GET', `/api/room-types/${r.id}`);
    const el = $(`[data-room="${r.id}"] .roomBody`);
    el.innerHTML = roomDetailHtml(detail);
  }
  root.onclick = e => handleRoomClick(e);
}

function roomDetailHtml(d) {
  const rules = d.rule_versions.map(rv => `
    <div class="rule">
      <strong>v${rv.version}</strong>（${rv.effective_from} ~ ${rv.effective_to || '长期'}）
      <code>${rv.version === d.effective_rule?.version ? '当前生效' : ''}</code>
      <pre>${esc(JSON.stringify(rv.rules, null, 2))}</pre>
    </div>`).join('');
  const feats = d.features.map(f => `
    <div class="rule">${f.kind === 'facility' ? '🛋 设施' : '🪟 窗景'}：
      <strong>${esc(f.label)}</strong>（${f.valid_from} ~ ${f.valid_to || '至今'}）
      <small>${esc(f.source_note)}</small></div>`).join('');
  return `<h3>入住规则修订</h3>${rules || '<p class="hint">暂无</p>'}
          <h3>设施 / 窗景（含有效期）</h3>${feats || '<p class="hint">暂无</p>'}`;
}

async function handleRoomClick(e) {
  const rid = e.target.dataset.rev || e.target.dataset.feat;
  if (!rid) return;
  try {
    if (e.target.dataset.rev) {
      const from = prompt('新生效日期 YYYY-MM-DD', '2026-10-03');
      const checkIn = prompt('入住时间', '15:00 后');
      const checkOut = prompt('退房时间', '次日 11:00 前');
      if (!from || !checkIn) return;
      await api('POST', `/api/room-types/${rid}/rules`, {
        effective_from: from,
        rules: { check_in: checkIn, check_out: checkOut,
                 pets: '按房型公示', smoking: '全面禁烟', note: 'Web 修订' } });
      toast('规则修订已生成新版本（旧版本保留）');
      await viewProperty();
    } else {
      const kind = prompt('facility 或 window_view', 'facility');
      const label = prompt('名称');
      if (!label) return;
      await api('POST', `/api/room-types/${rid}/features`, {
        kind, label, valid_from: '2026-01-01', source_note: 'Web 录入' });
      toast('设施/窗景已绑定房型与有效期');
      await viewProperty();
    }
  } catch (err) { toast(err.message, true); }
}

// ---------------------------------------------------------------- assets

async function viewAssets() {
  mountTpl('assets');
  await loadRooms();
  $('#aRoom').innerHTML = '<option value="">— 公共空间不选 —</option>' +
    state.rooms.map(r => `<option value="${r.id}">${esc(r.name)}</option>`).join('');
  await refreshAssets();
  bindUpload();
  $('#createAssetBtn').onclick = createAsset;
}

async function refreshAssets() {
  const d = await api('GET', `/api/assets?property_id=${state.propertyId}`);
  state.assets = d.assets;
  $('#assetTable tbody').innerHTML = d.assets.map(a => `
    <tr>
      <td>${a.id}</td>
      <td>${esc(a.title)} ${a.thumbnail_only ? '<span class="badge warn">仅缩略图</span>' : ''}</td>
      <td><span class="badge ${a.scope}">${a.scope === 'shared' ? '公共·' + esc(a.space_label) : '私有·' + esc(a.space_label)}</span></td>
      <td>v${a.current_version}</td>
      <td>${a.license ? (a.license.withdrawn ? '<span class="badge danger">已撤回</span>'
        : esc(a.license.holder)) : '<span class="badge warn">无授权</span>'}</td>
      <td><small>${a.version.source_status === 'unreachable'
        ? '<span class="badge danger">来源失效</span>' : esc(a.version.source_uri)}</small></td>
      <td>
        <button class="tiny" data-withdraw="${a.id}">撤回授权</button>
        <button class="tiny" data-dead="${a.id}">标记来源失效</button>
      </td>
    </tr>`).join('');
  $('#assetTable tbody').onclick = async e => {
    const id = e.target.dataset.withdraw || e.target.dataset.dead;
    if (!id) return;
    try {
      if (e.target.dataset.withdraw) {
        const reason = prompt('撤回原因', '授权方终止合作');
        await api('POST', `/api/assets/${id}/withdraw`, { reason });
        toast('授权已撤回：引用该版本的项目发布将被阻断；复制版按策略处理');
      } else {
        await api('POST', `/api/assets/${id}/source-status`, { status: 'unreachable' });
        toast('已标记照片来源失效');
      }
      await refreshAssets();
    } catch (err) { toast(err.message, true); }
  };
}

function bindUpload() {
  let session = null, bytes = null, failAt = Math.random() < 0.5 ? 1 : -1;
  $('#uploadBtn').onclick = async () => {
    const file = $('#fileInput').files[0];
    if (!file) return toast('请选择文件');
    try {
      const chunkSize = Math.max(64 * 1024, Math.ceil(file.size / 4));
      session = await api('POST', '/api/uploads', {
        filename: file.name, total_size: file.size, chunk_size });
      bytes = file;
      for (let i = 0; i < Math.ceil(file.size / chunkSize); i++) {
        if (i === failAt) { // simulate one transient failure -> user retries
          failAt = -1;
          $('#uploadProg').textContent = '分片 ' + i + ' 网络失败，点重试继续';
          return;
        }
        const blob = file.slice(i * chunkSize, Math.min(file.size, (i + 1) * chunkSize));
        await uploadChunk(session.upload_id, i, blob);
        $('#uploadProg').textContent =
          `已传 ${Math.round(100 * Math.min(file.size, (i + 1) * chunkSize) / file.size)}%`;
      }
      const done = await api('POST', `/api/uploads/${session.upload_id}/complete`);
      $('#aSha').value = done.sha256;
      $('#aSource').value = $('#aSource').value || 'media://upload/' + file.name;
      $('#uploadProg').textContent = '完成 sha256=' + done.sha256.slice(0, 10) + '…';
      toast('上传完成（重试分片幂等，完成时整体校验）');
    } catch (err) { toast(err.message, true); }
  };
  // "retry" = click upload button again; server reports received bytes.
}

async function uploadChunk(id, idx, blob) {
  const res = await fetch(`/api/uploads/${id}/chunks/${idx}`, {
    method: 'PUT', headers: { 'X-User-Id': state.userId },
    body: new Uint8Array(await blob.arrayBuffer()) });
  if (!res.ok) throw new Error('分片失败，可重试');
  return res.json();
}

async function createAsset() {
  try {
    const body = {
      property_id: state.propertyId, kind: $('#aKind').value,
      title: $('#aTitle').value || '未命名素材',
      scope: $('#aScope').value, room_type_id: +$('#aRoom').value || null,
      space_label: $('#aSpace').value || '未命名空间',
      source_uri: $('#aSource').value || 'media://unknown',
      sha256: $('#aSha').value,
      thumbnail_only: +$('#aThumb').value,
      license: { holder: $('#aLicHolder').value, valid_from: $('#aLicFrom').value },
    };
    await api('POST', '/api/assets', body);
    toast('素材已登记');
    await refreshAssets();
  } catch (err) { toast(err.message, true); }
}

// --------------------------------------------------------------- project

async function viewProject() {
  if (!state.projectId) {
    await viewHome();
    return toast('请先从首页打开一个项目');
  }
  mountTpl('project');
  await loadRooms();
  const d = await api('GET', `/api/projects/${state.projectId}`);
  state.timeline = d.timeline;
  state.baseVersion = d.latest_version;
  $('#projTitle').textContent = `${d.project.title}（编辑基准 v${state.baseVersion}）`;
  $$('.chBox').forEach(cb => {
    cb.checked = state.timeline.channels.includes(cb.value);
    cb.onchange = () => {
      state.timeline.channels = $$('.chBox').filter(x => x.checked).map(x => x.value);
    };
  });
  await refreshAssets();
  $('#addAsset').innerHTML = state.assets.map(a =>
    `<option value="${a.id}">${esc(a.title)} [${a.scope}] v${a.current_version}</option>`).join('');
  $('#addClipBtn').onclick = addClip;
  $('#checkBtn').onclick = runChecks;
  $('#publishBtn').onclick = publish;
  renderClips();
  await renderExports();
}

function renderClips() {
  $('#clips').innerHTML = state.timeline.items.map((c, i) => clipHtml(c, i)).join('')
    || '<p class="hint">时间线为空，从下方添加镜头。</p>';
  $('#clips').onclick = e => clipClick(e);
  $('#clips').oninput = e => clipInput(e);
}

function clipHtml(c, i) {
  const asset = state.assets.find(a => a.id === c.asset_id) || {};
  return `
  <div class="clip" data-idx="${i}">
    <div class="row">
      <strong>#${i + 1} ${esc(c.clip_id)}</strong>
      <span class="badge ${c.scope}">${c.scope === 'shared' ? '公共空间' : '房型私有'}</span>
      <span class="badge ${c.mode}">${c.mode === 'reference' ? '引用母素材' : '复制进项目'}</span>
      <span class="badge">${esc(asset.title || ('asset#' + c.asset_id))} v${c.asset_version}</span>
      <span class="spacer"></span>
      <button class="tiny" data-save="${i}">保存全部修改</button>
      <button class="tiny" data-copy="${i}">转为复制到项目</button>
      <button class="tiny danger" data-del="${i}">删除镜头</button>
    </div>
    <div class="ovl">
      <label>时长<input size="5" data-f="duration" value="${c.duration || 4}"></label>
      <label>字幕（自动进各渠道字幕带）<input size="34" data-f="caption" value="${esc(c.caption || '')}"></label>
    </div>
    <div class="ovl">内嵌必要提示（16:9 画布坐标，竖版裁切必须能保留）：
      <button class="tiny" data-prompt="${i}">+ 添加内嵌提示</button></div>
    ${(c.embedded_prompts || []).map((p, j) => `
      <div class="ovl" data-prompt-row="${j}">
        <input size="16" data-pf="label" value="${esc(p.label)}">
        x<input size="4" data-pf="x" value="${p.x}"> y<input size="4" data-pf="y" value="${p.y}">
        w<input size="4" data-pf="w" value="${p.w}"> h<input size="4" data-pf="h" value="${p.h}">
        <button class="tiny danger" data-rmprompt="${i}-${j}">移除</button>
      </div>`).join('')}
    <div class="ovl">卡片：
      <button class="tiny" data-rulecard="${i}">+ 规则卡片</button>
      <button class="tiny" data-featcard="${i}">+ 设施/窗景卡片</button>
    </div>
    ${(c.overlays || []).filter(o => o.type !== 'caption').map((o, j) => overlayHtml(c, o, j)).join('')}
  </div>`;
}

function overlayHtml(c, o, j) {
  const chRows = state.timeline.channels.map(ch => {
    const g = (o.channels || {})[ch] || { x: '', y: '', w: '', h: '' };
    return `<span class="ovl">${ch}:
      x<input size="4" data-geo="${ch}-x" data-oj="${j}" value="${g.x}">
      y<input size="4" data-geo="${ch}-y" data-oj="${j}" value="${g.y}">
      w<input size="4" data-geo="${ch}-w" data-oj="${j}" value="${g.w}">
      h<input size="4" data-geo="${ch}-h" data-oj="${j}" value="${g.h}"></span>`;
  }).join('');
  const bind = o.type === 'rule_card'
    ? `规则 v${o.rule_version_id}`
    : `${o.type === 'window_card' ? '窗景' : '设施'} #${o.feature_id}`;
  return `<div class="ovl" data-overlay="${j}" style="border:1px dashed #ccc;padding:6px;border-radius:6px;">
      <strong>${esc(o.label)}</strong> <small>${bind}（渠道安全区内坐标 0..1）</small><br>${chRows}
      <button class="tiny danger" data-rmover="${c.clip_id}-${j}">移除卡片</button></div>`;
}

function currentItem(idx) { return state.timeline.items[idx]; }

async function clipClick(e) {
  const t = e.target;
  const idx = +t.closest('.clip')?.dataset.idx;
  const c = currentItem(idx);
  try {
    if (t.dataset.prompt !== undefined && t.dataset.prompt !== '') {
      c.embedded_prompts = c.embedded_prompts || [];
      c.embedded_prompts.push({ label: '必要提示', x: 0.36, y: 0.25, w: 0.28, h: 0.12, essential: true });
      return renderClips();
    }
    if (t.dataset.rulecard !== undefined && t.dataset.rulecard !== '') {
      const room = state.rooms.find(r => r.id === c.room_type_id) || state.rooms[0];
      const d = await api('GET', `/api/room-types/${room.id}`);
      const rv = d.rule_versions.at(-1);
      c.overlays.push({ type: 'rule_card', label: '入住规则卡片',
        room_type_id: room.id, rule_version_id: rv.id, essential: true,
        channels: defaultGeos() });
      return renderClips();
    }
    if (t.dataset.featcard !== undefined && t.dataset.featcard !== '') {
      const room = state.rooms.find(r => r.id === c.room_type_id) || state.rooms[0];
      const d = await api('GET', `/api/room-types/${room.id}`);
      const f = d.features[0];
      if (!f) return toast('该房型还没有登记设施/窗景');
      c.overlays.push({ type: f.kind === 'window_view' ? 'window_card' : 'facility_card',
        label: f.label, room_type_id: room.id, feature_id: f.id, essential: false,
        channels: defaultGeos(0.3) });
      return renderClips();
    }
    const rp = t.dataset.rmprompt;
    if (rp) {
      const [, j] = rp.split('-').map(Number);
      c.embedded_prompts.splice(j, 1);
      return renderClips();
    }
    const ro = t.dataset.rmover;
    if (ro) {
      const [, j] = ro.split('-').map(Number);
      let n = 0;
      c.overlays = c.overlays.filter(o => o.type === 'caption' || n++ !== j);
      return renderClips();
    }
    if (t.dataset.copy !== undefined && t.dataset.copy !== '') return toCopy(idx);
    if (t.dataset.del !== undefined && t.dataset.del !== '') {
      state.timeline.items.splice(idx, 1);
      return saveTimeline();
    }
    if (t.dataset.save !== undefined && t.dataset.save !== '') return saveTimeline();
  } catch (err) { toast(err.message, true); }
}

function defaultGeos(y = 0.1) {
  const g = {};
  for (const ch of state.timeline.channels) {
    const info = state.channels.find(x => x.key === ch);
    // stay inside the tightest common inset
    const x = Math.max(0.1, info.inset_left + 0.02);
    g[ch] = { x, y, w: Math.min(0.8, 1 - 2 * x), h: 0.16 };
  }
  return g;
}

function clipInput(e) {
  const t = e.target;
  const wrap = t.closest('.clip');
  if (!wrap) return;
  const c = currentItem(+wrap.dataset.idx);
  if (t.dataset.f) c[t.dataset.f] = t.dataset.f === 'duration' ? +t.value : t.value;
  if (t.dataset.pf !== null) {
    const row = t.closest('[data-prompt-row]');
    const j = +row.dataset.promptRow;
    const key = t.dataset.pf;
    c.embedded_prompts[j][key] = ['x', 'y', 'w', 'h'].includes(key) ? +t.value : t.value;
  }
  if (t.dataset.geo) {
    const [ch, key] = t.dataset.geo.split('-');
    const j = +t.dataset.oj;
    const nonCaps = c.overlays.filter(o => o.type !== 'caption');
    nonCaps[j].channels = nonCaps[j].channels || {};
    nonCaps[j].channels[ch] = nonCaps[j].channels[ch] || {};
    nonCaps[j].channels[ch][key] = +t.value;
  }
}

async function addClip() {
  try {
    const asset = state.assets.find(a => a.id === +$('#addAsset').value);
    const clip = {
      clip_id: 'c' + Math.random().toString(36).slice(2, 8),
      asset_id: asset.id, asset_version: asset.current_version,
      mode: $('#addMode').value, scope: asset.scope,
      room_type_id: asset.room_type_id, duration: +$('#addDur').value || 4,
      caption: $('#addCaption').value, embedded_prompts: [],
      overlays: [{ type: 'caption', label: '字幕', essential: true }],
    };
    state.timeline.items.push(clip);
    await saveTimeline();
    renderClips();
  } catch (err) { toast(err.message, true); }
}

async function toCopy(idx) {
  try {
    const c = currentItem(idx);
    const r = await api('POST', `/api/projects/${state.projectId}/copy-clip`,
                       { clip_id: c.clip_id });
    state.baseVersion = r.version;
    c.mode = 'copy';
    toast('已转为复制进项目：母素材后续替换/撤回不影响本副本');
    renderClips();
  } catch (err) { toast(err.message, true); }
}

async function saveTimeline() {
  try {
    const r = await api('POST', `/api/projects/${state.projectId}/timeline`, {
      base_version: state.baseVersion, timeline: state.timeline });
    state.baseVersion = r.version;
    $('#projTitle').textContent =
      $('#projTitle').textContent.replace(/v\d+/, 'v' + r.version);
    toast('已保存为 v' + r.version);
  } catch (err) {
    if (err.status === 409) {
      toast(`版本冲突：同事已保存 v${err.payload.current_version}，请刷新合并后再保存（不会覆盖对方）`, true);
    } else if (err.status === 422) {
      toast('校验失败：\n' + (err.payload.issues || [])
        .filter(i => i.severity === 'error').map(i => `· ${i.detail || i.code}`).join('\n'), true);
    } else toast(err.message, true);
  }
}

async function runChecks() {
  await saveTimelineQuiet();
  const r = await api('GET', `/api/projects/${state.projectId}/checks`);
  renderCheckPanel(r);
}

async function saveTimelineQuiet() {
  try {
    const r = await api('POST', `/api/projects/${state.projectId}/timeline`, {
      base_version: state.baseVersion, timeline: state.timeline });
    state.baseVersion = r.version;
  } catch (err) {
    if (err.status !== 409) throw err;
    throw new Error(`有同事先保存了 v${err.payload.current_version}，请刷新页面后再检查`);
  }
}

function renderCheckPanel(r) {
  const panel = $('#checkPanel');
  panel.classList.remove('hidden');
  const rows = r.issues.length ? r.issues.map(i =>
    `<div class="issue ${i.severity}"><code>${i.code}</code>
      [${i.channel || '全局'}] ${esc(i.detail)} <small>(${esc(i.clip_id)})</small></div>`
  ).join('') : '<div class="issue">全部依赖与渠道安全区检查通过 ✅</div>';
  panel.innerHTML = `<h3>发布前检查（as_of=${r.as_of}，error 阻断 / warning 提示）</h3>${rows}`;
}

async function publish() {
  try {
    await saveTimelineQuiet();
    const r = await api('POST', `/api/projects/${state.projectId}/publish`, {});
    if (r.warnings?.length) toast('已发布（有 ' + r.warnings.length + ' 条提示）');
    else toast('发布成功，导出已生成');
    await renderExports();
  } catch (err) {
    if (err.status === 422) renderCheckPanel(err.payload.checks || { issues: [] });
    toast('发布被阻断：' + err.message, true);
  }
}

async function renderExports() {
  const d = await api('GET', `/api/projects/${state.projectId}/exports`);
  $('#exportList').innerHTML = d.exports.map(e => `
    <div class="rule">
      导出 #${e.id} · 项目版本行 #${e.project_version_id}
      · 渠道 ${e.channels_json.join(' / ')}
      · 制作于 ${new Date(e.created_at * 1000).toLocaleString()}
      <a class="out" href="/output.html?eid=${e.id}">查看产出（字幕/镜头清单/接触印片/预览）</a>
    </div>`).join('') || '<p class="hint">还没有导出。</p>';
}

boot().catch(e => toast(e.message, true));
