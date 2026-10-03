'use strict';
/* 民宿房源视频编辑台 前端 SPA */
const state = {
  users: [], me: null,
  properties: [], prop: null,
  roomTypes: [], rt: null,
  materials: [],
  projects: [], proj: null, projDetail: null,
  channels: [],
};

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

async function api(method, path, body, headers = {}) {
  const r = await fetch(path, {
    method,
    headers: { 'content-type': 'application/json', 'x-user-id': state.me, ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(data.error || r.statusText); e.status = r.status; e.data = data; throw e; }
  return data;
}

function toast(msg, isErr = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = isErr ? 'err' : '';
  t.hidden = false;
  setTimeout(() => { t.hidden = true; }, 3200);
}
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// ---------- 启动 ----------
async function boot() {
  state.users = await api('POST', '/api/dev/seed-users');
  state.me = state.users[0].id;
  const sel = $('#user-select');
  sel.innerHTML = state.users.map((u) => `<option value="${u.id}">${esc(u.name)}</option>`).join('');
  sel.onchange = () => { state.me = sel.value; toast(`已切换为 ${sel.selectedOptions[0].text}`); refreshAll(); };
  await refreshAll();
}

async function refreshAll() {
  await Promise.all([loadProperties(), loadChannels(), loadHomepage()]);
  if (state.prop) { await loadRoomTypes(); await loadMaterials(); }
  await loadProjects();
}

// ---------- 标签页 ----------
$$('#tabs button').forEach((b) => {
  b.onclick = () => {
    $$('#tabs button').forEach((x) => x.classList.toggle('active', x === b));
    $$('section.tab').forEach((s) => s.classList.toggle('active', s.id === `tab-${b.dataset.tab}`));
  };
});

// ---------- 房源 ----------
async function loadProperties() {
  state.properties = await api('GET', '/api/properties');
  if (!state.prop && state.properties.length) state.prop = state.properties[0].id;
  $('#prop-list').innerHTML = state.properties.map((p) =>
    `<li class="${p.id === state.prop ? 'selected' : ''}" data-id="${p.id}">
       <span>🏠 ${esc(p.name)}</span><span class="muted">${p.id}</span></li>`).join('');
  $$('#prop-list li').forEach((li) => {
    li.onclick = async () => {
      state.prop = li.dataset.id; state.rt = null; state.proj = null;
      await loadProperties(); await loadRoomTypes(); await loadMaterials();
    };
  });
  const cur = state.properties.find((p) => p.id === state.prop);
  $('#rt-prop-name').textContent = cur ? `（${cur.name}）` : '';
  $('#mat-prop-name').textContent = cur ? `（${cur.name}）` : '';
  $('#roomtype-panel').hidden = !cur;
}

$('#btn-add-prop').onclick = async () => {
  const name = $('#prop-name').value.trim();
  if (!name) return toast('请输入房源名称', true);
  await api('POST', '/api/properties', { name });
  $('#prop-name').value = '';
  await loadProperties(); await loadRoomTypes(); await loadMaterials();
  toast('房源已创建');
};

// ---------- 房型 + 设施/窗景/规则 ----------
async function loadRoomTypes() {
  if (!state.prop) return;
  state.roomTypes = await api('GET', `/api/properties/${state.prop}/room-types`);
  if (!state.rt && state.roomTypes.length) state.rt = state.roomTypes[0].id;
  const html = [];
  for (const rt of state.roomTypes) {
    const [fac, views, rules] = await Promise.all([
      api('GET', `/api/room-types/${rt.id}/facilities`),
      api('GET', `/api/room-types/${rt.id}/window-views`),
      api('GET', `/api/room-types/${rt.id}/rules`),
    ]);
    html.push(`<div class="shot">
      <div class="head"><b>${esc(rt.name)}</b> <span class="muted">描述v${rt.description_version}：${esc(rt.description)}</span>
        <span><button class="small-btn" onclick="editRtDesc('${rt.id}')">改描述</button>
        <button class="small-btn" onclick="addBinding('${rt.id}','facilities')">+设施</button>
        <button class="small-btn" onclick="addBinding('${rt.id}','window-views')">+窗景</button>
        <button class="small-btn" onclick="addRule('${rt.id}')">+规则/修订</button></span></div>
      <div class="muted" style="margin-top:6px">
        设施：${fac.map((f) => `${esc(f.name)}[${f.effective_from}~${f.effective_to || '∞'}]`).join('、') || '无'}<br>
        窗景：${views.map((v) => `${esc(v.view_type)}(${esc(v.description)})[${v.effective_from}~]`).join('、') || '无'}<br>
        规则：${rules.map((r) => `v${r.revision}「${esc(r.title)}：${esc(r.text)}」${r.effective_from}~${r.effective_to || '∞'}${r.superseded_by ? '(已被修订)' : ''}`).join('<br>') || '无'}
      </div></div>`);
  }
  $('#rt-list').innerHTML = html.join('') || '<p class="muted">暂无房型</p>';
}

$('#btn-add-rt').onclick = async () => {
  const name = $('#rt-name').value.trim();
  if (!name) return toast('请输入房型名称', true);
  await api('POST', `/api/properties/${state.prop}/room-types`, { name, description: $('#rt-desc').value });
  $('#rt-name').value = ''; $('#rt-desc').value = '';
  await loadRoomTypes(); toast('房型已添加');
};

window.editRtDesc = async (id) => {
  const desc = prompt('新的房型描述（旧导出快照不会变化）：');
  if (desc == null) return;
  await api('PATCH', `/api/room-types/${id}`, { description: desc });
  await loadRoomTypes(); toast('描述已更新（版本+1）');
};

window.addBinding = async (rtId, kind) => {
  const from = prompt('生效日期 effective_from（YYYY-MM-DD）：', '2026-01-01');
  if (!from) return;
  const to = prompt('失效日期 effective_to（留空=长期）：') || null;
  let body = { effective_from: from, effective_to: to };
  if (kind === 'facilities') { const name = prompt('设施名称：'); if (!name) return; body.name = name; }
  else { const vt = prompt('窗景类型（mountain/sea/city…）：'); if (!vt) return; body.view_type = vt; body.description = prompt('窗景描述：') || ''; }
  await api('POST', `/api/room-types/${rtId}/${kind}`, body);
  await loadRoomTypes(); toast('已绑定（带有效期）');
};

window.addRule = async (rtId) => {
  const rules = await api('GET', `/api/room-types/${rtId}/rules`);
  const revisable = rules.filter((r) => !r.effective_to);
  const revises = revisable.length && confirm('是否修订现有规则？\n确定=修订最新一条，取消=新建规则')
    ? revisable[revisable.length - 1].id : null;
  const title = prompt('规则标题（如：入住时间）：'); if (!title) return;
  const text = prompt('规则内容（如：14:00 后入住）：'); if (!text) return;
  const from = prompt('生效日期：', '2026-10-01'); if (!from) return;
  const r = await api('POST', `/api/room-types/${rtId}/rules`, { title, text, effective_from: from, revises });
  await loadRoomTypes();
  toast(revises ? `已修订为 v${r.revision}，旧规则同日截断` : '规则已创建');
};

// ---------- 素材库 ----------
async function loadMaterials() {
  if (!state.prop) return;
  state.materials = await api('GET', `/api/properties/${state.prop}/materials`);
  const rtSel = $('#mat-roomtype');
  rtSel.innerHTML = state.roomTypes.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join('');
  $('#clip-material').innerHTML = state.materials.map((m) => `<option value="${m.id}">${esc(m.title)} (v${m.version})</option>`).join('');
  $('#mat-table tbody').innerHTML = state.materials.map((m) => `<tr>
    <td>${esc(m.title)} <span class="muted">${m.kind}</span></td>
    <td>${m.scope === 'property_shared' ? '<span class="tag blue">公共</span>' : `<span class="tag">私有·${esc(roomTypeName(m.room_type_id))}</span>`}</td>
    <td>${m.space_label === 'private' ? '<span class="tag orange">私有空间</span>' : '<span class="tag">公共空间</span>'}</td>
    <td>v${m.version}</td>
    <td>${m.license.status === 'active' ? '<span class="tag green">授权中</span>' : '<span class="tag red">已撤回</span>'}</td>
    <td>${m.status === 'active' ? '<span class="tag green">正常</span>' : '<span class="tag red">来源失效</span>'}</td>
    <td>
      <button class="small-btn" onclick="tryPrivate('${m.id}')">标为私有</button>
      <button class="small-btn" onclick="updateMaster('${m.id}')">更新母版</button>
      <button class="small-btn" onclick="revokeLic('${m.id}')">撤回授权</button>
      <button class="small-btn" onclick="invalidateSrc('${m.id}')">来源失效</button>
    </td></tr>`).join('');
}
const roomTypeName = (id) => (state.roomTypes.find((r) => r.id === id) || {}).name || id || '-';

$('#mat-scope').onchange = () => { $('#mat-roomtype').hidden = $('#mat-scope').value !== 'room_private'; };

$('#btn-add-mat').onclick = async () => {
  const title = $('#mat-title').value.trim();
  if (!title) return toast('请输入素材标题', true);
  const scope = $('#mat-scope').value;
  try {
    await api('POST', `/api/properties/${state.prop}/materials`, {
      title, kind: $('#mat-kind').value, scope,
      room_type_id: scope === 'room_private' ? $('#mat-roomtype').value : null,
      tags: $('#mat-tags').value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      space_label: scope === 'room_private' ? 'private' : 'shared',
    });
    $('#mat-title').value = '';
    await loadMaterials(); toast('素材已上传');
  } catch (e) { toast(e.message, true); }
};

window.tryPrivate = async (id) => {
  const rtId = state.roomTypes[0] && state.roomTypes[0].id;
  try {
    await api('POST', `/api/materials/${id}/space-label`, { space_label: 'private', room_type_id: rtId });
    toast('已标为私有空间');
  } catch (e) { toast(`被拒绝：${e.message}`, true); }
  await loadMaterials();
};
window.updateMaster = async (id) => {
  const r = await api('PATCH', `/api/materials/${id}`, { content_hash: `h_${Date.now()}` });
  toast(`母版已更新到 v${r.material.version}，${r.affected_reference_clips.length} 个引用片段标记为「有新版」`);
  await loadMaterials(); if (state.proj) await openProject(state.proj);
};
window.revokeLic = async (id) => { await api('POST', `/api/materials/${id}/revoke-license`); await loadMaterials(); toast('授权已撤回（引用与复制件发布时都会被拦截）'); };
window.invalidateSrc = async (id) => { await api('POST', `/api/materials/${id}/invalidate-source`); await loadMaterials(); toast('来源已标记失效'); };

// ---------- 项目 / 时间线 ----------
async function loadProjects() {
  state.projects = await api('GET', '/api/projects').catch(() => []);
  renderProjects();
}

function renderProjects() {
  $('#proj-list').innerHTML = (state.projects || []).map((p) =>
    `<li class="${p.id === state.proj ? 'selected' : ''}" data-id="${p.id}">
      <span>🎬 ${esc(p.name)}</span>
      <span><span class="tag ${p.status === 'published' ? 'green' : 'blue'}">${p.status}</span>
      <button class="small-btn" onclick="openProject('${p.id}')">打开</button></span></li>`).join('');
}

$('#btn-add-proj').onclick = async () => {
  const name = $('#proj-name').value.trim();
  if (!name || !state.prop) return toast('请选择房源并输入项目名', true);
  const p = await api('POST', '/api/projects', { property_id: state.prop, name });
  $('#proj-name').value = '';
  await loadProjects(); await openProject(p.id);
};

window.openProject = async (id) => {
  try {
    const detail = await api('GET', `/api/projects/${id}`);
    state.proj = id; state.projDetail = detail;
    renderProjects();
    $('#timeline-panel').hidden = false;
    $('#publish-panel').hidden = false;
    $('#tl-proj-name').textContent = `（${detail.name} · 我的角色:${detail.role}）`;
    $('#pub-proj-name').textContent = `（${detail.name}）`;
    renderShots(detail); renderClips(detail);
    await loadExports(); await loadSafePanel();
  } catch (e) {
    toast(`打开失败：${e.message}（权限可能已变化）`, true);
  }
};

function renderShots(d) {
  $('#tl-script-ver').textContent = `脚本 v${d.script_version}`;
  $('#shot-list').innerHTML = d.shots.map((s) => `<div class="shot">
    <div class="head"><b>#${s.idx} ${esc(s.name)}</b>
      <span class="muted">${s.duration}s · 版本v${s.version} · 最后编辑:${esc(s.updated_by)}</span>
      <span><button class="small-btn" onclick="editShot('${s.id}',${s.version})">改名/时长</button>
      <button class="small-btn" onclick="addOverlay('${s.id}',${s.version},'subtitle')">+字幕</button>
      <button class="small-btn" onclick="addOverlay('${s.id}',${s.version},'notice')">+必要提示</button></span></div>
    <div>${(s.overlays || []).map((o) => `<span class="overlay-chip ${o.required ? 'required' : ''}">${o.required ? '❗' : '💬'}${esc(o.text)} @(${(o.box.x * 100) | 0}%,${(o.box.y * 100) | 0}%)</span>`).join('')}</div>
  </div>`).join('') || '<p class="muted">暂无镜头</p>';
}

$('#btn-add-shot').onclick = async () => {
  const name = $('#shot-name').value.trim();
  if (!name) return toast('请输入镜头名', true);
  try {
    await api('POST', `/api/projects/${state.proj}/shots`, { name, duration: Number($('#shot-duration').value) || 5 });
    $('#shot-name').value = '';
    await openProject(state.proj);
  } catch (e) { toast(e.message, true); }
};

window.editShot = async (id, ver) => {
  const duration = Number(prompt('新时长（秒）：'));
  if (!duration) return;
  try {
    await api('PATCH', `/api/shots/${id}`, { base_version: ver, duration });
    await openProject(state.proj);
  } catch (e) {
    if (e.status === 409) {
      toast('冲突：别人已先修改该镜头，已刷新到最新版本', true);
      await openProject(state.proj);
    } else toast(e.message, true);
  }
};

window.addOverlay = async (shotId, ver, type) => {
  const text = prompt(type === 'subtitle' ? '字幕文本：' : '必要提示文本（如：不可退订）：');
  if (!text) return;
  const x = Number(prompt('水平位置 x%（0-100，竖版只保留中间约32%宽）：', '40')) / 100;
  const y = Number(prompt('垂直位置 y%（0-100）：', '80')) / 100;
  const d = state.projDetail;
  const shot = d.shots.find((s) => s.id === shotId);
  const overlays = [...(shot.overlays || []), {
    id: `ov_${Date.now()}`, type, text, required: type === 'notice',
    box: { x, y, w: 0.2, h: 0.08 },
  }];
  try {
    await api('PATCH', `/api/shots/${shotId}`, { base_version: ver, overlays });
    await openProject(state.proj);
  } catch (e) {
    if (e.status === 409) { toast('镜头已被他人修改，请重试', true); await openProject(state.proj); }
    else toast(e.message, true);
  }
};

$('#btn-add-clip').onclick = async () => {
  const material_id = $('#clip-material').value;
  if (!material_id) return toast('素材库为空', true);
  try {
    await api('POST', `/api/projects/${state.proj}/clips`, { material_id, source_mode: $('#clip-mode').value });
    await openProject(state.proj);
  } catch (e) { toast(e.message, true); }
};

function renderClips(d) {
  const freshTag = { ok: '<span class="tag green">正常</span>', stale: '<span class="tag orange">有新版</span>',
    license_revoked: '<span class="tag red">授权撤回</span>', source_missing: '<span class="tag red">来源失效</span>',
    material_deleted: '<span class="tag red">素材已删</span>' };
  $('#clip-table tbody').innerHTML = d.clips.map((c) => {
    const m = state.materials.find((x) => x.id === c.material_id);
    return `<tr><td>${esc(m ? m.title : c.material_id)}</td>
      <td>${c.source_mode === 'reference' ? '<span class="tag blue">引用</span>' : '<span class="tag">复制</span>'}</td>
      <td>v${c.material_version}</td>
      <td>${freshTag[c.freshness] || c.freshness}</td>
      <td>${c.source_mode === 'reference' && c.freshness === 'stale' ? `<button class="small-btn" onclick="followMaster('${c.id}')">跟随母版</button>` : ''}</td></tr>`;
  }).join('');
}

window.followMaster = async (clipId) => {
  await api('POST', `/api/clips/${clipId}/follow-master`);
  await openProject(state.proj); toast('已跟随母版最新版本');
};

$('#btn-save-version').onclick = async () => {
  const v = await api('POST', `/api/projects/${state.proj}/versions`);
  toast(`版本快照 v${v.version_no} 已保存`);
};

// ---------- 媒体工作器 ----------
$$('.btn-job').forEach((b) => {
  b.onclick = async () => {
    const { job_id } = await api('POST', `/api/projects/${state.proj}/jobs/${b.dataset.job}`);
    toast('任务已提交媒体工作器…');
    const out = $('#job-output'); out.hidden = false; out.textContent = '处理中…';
    const poll = setInterval(async () => {
      const j = await api('GET', `/api/jobs/${job_id}`);
      if (j.status === 'done') { clearInterval(poll); out.textContent = JSON.stringify(j.result, null, 2); }
      else if (j.status === 'failed') { clearInterval(poll); out.textContent = `失败：${j.error}`; }
    }, 150);
  };
});

// ---------- 渠道与安全区 ----------
async function loadChannels() {
  state.channels = await api('GET', '/api/channels');
  $('#ch-list').innerHTML = state.channels.map((c) =>
    `<li><span>${c.aspect_ratio === '9:16' ? '📱' : '🖥'} ${esc(c.name)}（${c.aspect_ratio}，安全区 ${c.safe_area.top}/${c.safe_area.right}/${c.safe_area.bottom}/${c.safe_area.left}%）</span></li>`).join('');
}

$('#btn-add-ch').onclick = async () => {
  const name = $('#ch-name').value.trim();
  if (!name) return toast('请输入渠道名', true);
  const [top, right, bottom, left] = $('#ch-safe').value.split(',').map(Number);
  await api('POST', '/api/channels', { name, aspect_ratio: $('#ch-aspect').value, safe_area: { top, right, bottom, left } });
  await loadChannels(); toast('渠道已创建');
};

async function loadSafePanel() {
  if (!state.proj) return;
  $('#safe-panel').hidden = false;
  const d = state.projDetail;
  const boundIds = new Set((await api('GET', `/api/projects/${state.proj}/channels`)).map((b) => b.channel_id));
  const binds = await Promise.all(state.channels.map(async (c) => {
    const bound = boundIds.has(c.id);
    const checks = bound ? await api('GET', `/api/projects/${state.proj}/channels/${c.id}/check`) : [];
    return { c, bound, latest: checks[0] };
  }));
  $('#bind-list').innerHTML = binds.map(({ c, bound, latest }) => `<div class="row" style="justify-content:space-between">
    <span>${c.aspect_ratio === '9:16' ? '📱' : '🖥'} ${esc(c.name)}
      ${latest ? (latest.ok ? `<span class="tag green">脚本v${latest.script_version} 通过</span>` : `<span class="tag red">脚本v${latest.script_version} 违规×${latest.violations.length}</span>`) : '<span class="tag">未检查</span>'}
      ${latest && latest.script_version !== d.script_version ? '<span class="tag orange">脚本已变更，需重检</span>' : ''}</span>
    <span>${bound ? `<button class="small-btn" onclick="runCheck('${c.id}')">执行安全区检查</button>` : `<button class="small-btn" onclick="bindChannel('${c.id}')">绑定渠道</button>`}</span>
  </div>`).join('');
  const bad = binds.find((b) => b.latest && !b.latest.ok);
  $('#safe-result').innerHTML = bad
    ? bad.latest.violations.map((v) => `<div class="violation">⚠️ ${esc(v.detail)}（${v.reason}）</div>`).join('')
    : (binds.some((b) => b.latest) ? '<div class="ok-box">✅ 最近检查均通过</div>' : '');
}

window.bindChannel = async (cid) => { await api('POST', `/api/projects/${state.proj}/channels/${cid}/bind`); await loadSafePanel(); };
window.runCheck = async (cid) => {
  const { job_id } = await api('POST', `/api/projects/${state.proj}/channels/${cid}/check`);
  const poll = setInterval(async () => {
    const j = await api('GET', `/api/jobs/${job_id}`);
    if (j.status === 'done') {
      clearInterval(poll);
      toast(j.result.ok ? '✅ 安全区检查通过' : `❌ ${j.result.violations.length} 处必要提示被裁切/越界`, !j.result.ok);
      await loadSafePanel();
    }
  }, 150);
};

// ---------- 发布 / 导出 / 首页游标 ----------
$('#btn-publish').onclick = async () => {
  try {
    const r = await api('POST', `/api/projects/${state.proj}/publish`);
    $('#publish-result').innerHTML = `<div class="ok-box">✅ 发布成功：版本 v${r.version.version_no}，导出 ${r.exports.length} 份（不可变快照）</div>`;
    await loadExports(); await loadProjects();
  } catch (e) {
    const ps = (e.data && e.data.problems) || [];
    $('#publish-result').innerHTML = `<div class="violation">🚫 发布被拦截：<br>${ps.map((p) => `• ${esc(p.detail || p.type)}`).join('<br>')}</div>`;
  }
};

$('#btn-set-cursor').onclick = async () => {
  const versions = await api('GET', `/api/projects/${state.proj}/versions`);
  if (!versions.length) return toast('请先保存版本快照', true);
  const latest = versions[versions.length - 1];
  await api('PUT', '/api/homepage/cursor', { project_id: state.proj, version_id: latest.id });
  await loadHomepage(); toast(`首页游标已指向 v${latest.version_no}`);
};

async function loadHomepage() {
  const list = await api('GET', '/api/homepage');
  $('#homepage-list').innerHTML = list.length ? list.map((c) =>
    `<div class="row" style="justify-content:space-between"><span>📌 ${esc(c.project_name)} · 固定展示 v${c.version_no}</span>
    <span>${c.dependency_ok ? '<span class="tag green">依赖健康</span>' : `<span class="tag red">依赖异常×${c.dependency_problems.length}</span>`}</span></div>`).join('')
    : '<p class="muted">尚未设置游标</p>';
}

async function loadExports() {
  if (!state.proj) return;
  const exps = await api('GET', `/api/projects/${state.proj}/exports`);
  $('#export-list').innerHTML = exps.length ? exps.map((e) => {
    const rtIds = Object.keys(e.room_type_descriptions);
    return `<div class="shot"><div class="head"><b>导出 ${e.id}</b><span class="muted">v${e.version_no} · ${e.produced_at.slice(0, 19).replace('T', ' ')}</span></div>
      <div class="muted" style="margin-top:6px">
        制作时房型描述：${rtIds.map((id) => `「${esc(e.room_type_descriptions[id].description)}」(描述v${e.room_type_descriptions[id].description_version})`).join('；')}<br>
        制作时规则：${e.rules.map((r) => `${esc(r.title)}:${esc(r.text)}(v${r.revision})`).join('；') || '无'}<br>
        素材授权@制作时：${e.materials.map((m) => `${m.material_id}=${m.license_status_at_production}`).join('，') || '无'}<br>
        周边推荐：${e.nearby_recommendations.map((n) => esc(n.text)).join('；') || '无'}${e.nearby_dropped_for_no_source ? `（${e.nearby_dropped_for_no_source} 条无来源被丢弃）` : ''}
      </div></div>`;
  }).join('') : '<p class="muted">暂无导出</p>';
}

// ---------- 上传重试演示 ----------
$('#btn-upload-demo').onclick = async () => {
  if (!state.proj) return toast('请先打开项目', true);
  const log = $('#upload-log'); log.hidden = false; log.textContent = '';
  const say = (s) => { log.textContent += s + '\n'; };
  const key = `demo-${Date.now()}`;
  say('1) 创建上传会话（3 块）…');
  const s = await api('POST', '/api/uploads', { project_id: state.proj, filename: 'room.mp4', total_chunks: 3, idempotency_key: key });
  say(`   会话 ${s.id}`);
  say('2) 网络抖动，重复创建同 key 会话…');
  const s2 = await api('POST', '/api/uploads', { project_id: state.proj, filename: 'room.mp4', total_chunks: 3, idempotency_key: key });
  say(`   去重命中：${s2.deduplicated}（同一会话 ${s2.id}）`);
  say('3) 上传块0，超时后原样重试…');
  await api('PUT', `/api/uploads/${s.id}/chunks/0`, {}, { 'x-chunk-key': 'ck0' });
  const retry = await api('PUT', `/api/uploads/${s.id}/chunks/0`, {}, { 'x-chunk-key': 'ck0' });
  say(`   重试结果：deduplicated=${retry.deduplicated}`);
  say('4) 补齐块1、块2 并完成…');
  await api('PUT', `/api/uploads/${s.id}/chunks/1`, {}, { 'x-chunk-key': 'ck1' });
  await api('PUT', `/api/uploads/${s.id}/chunks/2`, {}, { 'x-chunk-key': 'ck2' });
  const done = await api('POST', `/api/uploads/${s.id}/complete`);
  say(`   状态：${done.status} ✅`);
};

// ---------- 恢复上次项目 ----------
$('#btn-resume').onclick = async () => {
  try {
    const r = await api('GET', '/api/session/last-project');
    toast(`已恢复上次项目「${r.project.name}」（角色：${r.role}）`);
    await openProject(r.project.id);
  } catch (e) {
    toast(`恢复失败：${e.data.detail || e.message}`, true);
  }
};

boot().catch((e) => toast(`初始化失败：${e.message}`, true));
