'use strict';
/**
 * 验收测试：
 *  1. 入住规则修订（新旧版本可追溯、按日期生效）
 *  2. 照片来源失效 → 发布被拦截，旧导出保留制作时信息
 *  3. 两人调整同一镜头 → 乐观锁 409
 *  4. 上传重试（会话级 + 块级幂等）
 *  5. 恢复上次项目时权限已改变 → 403
 *  6. 大厅公共素材不能被标为房间私有空间
 *  7. 竖版裁切安全区检查（不能复用横版结论）
 *  8. 首页游标引用版本 + 发布前依赖重检
 *  9. 旧导出不套用当前房型描述；无来源周边推荐不生成
 * 10. 引用 vs 复制：母素材更新 / 授权撤回 / 历史复现
 */
const { test, before, after } = require('node:test');
const assert = require('node:assert');
const { createStore } = require('../server/store');
const { createWorker } = require('../server/worker');
const { createApp } = require('../server/app');

let base, store, worker, server;
let owner, editor, viewer;

function api(user, method, path, body, headers = {}) {
  return fetch(`${base}${path}`, {
    method,
    headers: {
      'content-type': 'application/json',
      ...(user ? { 'x-user-id': user } : {}),
      ...headers,
    },
    body: body ? JSON.stringify(body) : undefined,
  }).then(async (r) => ({ status: r.status, body: await r.json() }));
}

async function waitJob(jobId) {
  for (let i = 0; i < 100; i++) {
    const { body } = await api(owner, 'GET', `/api/jobs/${jobId}`);
    if (body.status === 'done' || body.status === 'failed') return body;
    await new Promise((r) => setTimeout(r, 20));
  }
  throw new Error('job timeout');
}

before(async () => {
  store = createStore(null); // 内存库
  worker = createWorker(store, { tickMs: 5 });
  const app = createApp(store, worker);
  await new Promise((resolve) => { server = app.listen(0, resolve); });
  base = `http://127.0.0.1:${server.address().port}`;
  server.unref();
  const users = (await api(null, 'POST', '/api/dev/seed-users')).body;
  [owner, editor, viewer] = users.map((u) => u.id);
});

/** 公共脚手架：房源 + 两房型 + 项目 */
async function scaffold() {
  const prop = (await api(owner, 'POST', '/api/properties', { name: '山景民宿' })).body;
  const rt1 = (await api(owner, 'POST', `/api/properties/${prop.id}/room-types`, { name: '山景大床房', description: '带阳台，看日出' })).body;
  const rt2 = (await api(owner, 'POST', `/api/properties/${prop.id}/room-types`, { name: '庭院标间', description: '一层，近花园' })).body;
  const proj = (await api(owner, 'POST', '/api/projects', { property_id: prop.id, name: '秋季推广片' })).body;
  await api(owner, 'POST', `/api/projects/${proj.id}/acl`, { user_id: editor, role: 'editor' });
  return { prop, rt1, rt2, proj };
}

// ---------- 6. 大厅素材不能被标为每间房的私有空间 ----------
test('大厅公共素材不能被标为房间私有空间', async () => {
  const { prop, rt1 } = await scaffold();
  const lobby = (await api(owner, 'POST', `/api/properties/${prop.id}/materials`, {
    title: '大厅全景', scope: 'property_shared', tags: ['lobby'],
  })).body;
  assert.equal(lobby.scope, 'property_shared');

  // 尝试标为某房型私有 → 拒绝
  const r1 = await api(owner, 'POST', `/api/materials/${lobby.id}/space-label`, { space_label: 'private', room_type_id: rt1.id });
  assert.equal(r1.status, 409);
  assert.match(r1.body.error, /公共素材/);

  // 公共素材创建时也不允许绑定单一房型
  const r2 = await api(owner, 'POST', `/api/properties/${prop.id}/materials`, {
    title: '走廊', scope: 'property_shared', room_type_id: rt1.id,
  });
  assert.equal(r2.status, 400);

  // 房间私有素材（卫生间）正常标注
  const bath = (await api(owner, 'POST', `/api/properties/${prop.id}/materials`, {
    title: '卫生间', scope: 'room_private', room_type_id: rt1.id, tags: ['bathroom'], space_label: 'private',
  })).body;
  assert.equal(bath.space_label, 'private');
});

// ---------- 设施/窗景/规则绑定房型 + 有效日期 ----------
test('设施、窗景、规则绑定具体房型及有效日期', async () => {
  const { rt1, rt2 } = await scaffold();
  await api(owner, 'POST', `/api/room-types/${rt1.id}/facilities`, { name: '投影仪', effective_from: '2026-01-01', effective_to: '2026-12-31' });
  await api(owner, 'POST', `/api/room-types/${rt1.id}/window-views`, { view_type: 'mountain', description: '东向山景', effective_from: '2026-01-01' });
  await api(owner, 'POST', `/api/room-types/${rt2.id}/facilities`, { name: '麻将桌', effective_from: '2026-06-01' });

  // 按日期查询：投影仪在 2026-06 有效，2027 失效
  const f1 = (await api(owner, 'GET', `/api/room-types/${rt1.id}/facilities?at=2026-06-01`)).body;
  const f2 = (await api(owner, 'GET', `/api/room-types/${rt1.id}/facilities?at=2027-01-01`)).body;
  assert.equal(f1.length, 1);
  assert.equal(f2.length, 0);

  // 房型隔离：rt2 看不到 rt1 的投影仪
  const f3 = (await api(owner, 'GET', `/api/room-types/${rt2.id}/facilities?at=2026-06-01`)).body;
  assert.deepEqual(f3.map((f) => f.name), ['麻将桌']);
});

// ---------- 1. 入住规则修订 ----------
test('入住规则修订：新版生效、旧版截断且可追溯', async () => {
  const { rt1 } = await scaffold();
  const v1 = (await api(owner, 'POST', `/api/room-types/${rt1.id}/rules`, {
    title: '入住时间', text: '15:00 后入住', effective_from: '2026-01-01',
  })).body;
  assert.equal(v1.revision, 1);

  // 修订：10月起改为 14:00
  const v2 = (await api(owner, 'POST', `/api/room-types/${rt1.id}/rules`, {
    title: '入住时间', text: '14:00 后入住', effective_from: '2026-10-01', revises: v1.id,
  })).body;
  assert.equal(v2.revision, 2);
  assert.equal(v2.supersedes, v1.id);

  // 旧规则被截断到 2026-10-01，且可追溯
  const old = (await api(owner, 'GET', `/api/room-types/${rt1.id}/rules`)).body.find((r) => r.id === v1.id);
  assert.equal(old.effective_to, '2026-10-01');
  assert.equal(old.superseded_by, v2.id);

  // 按日期解析：9月用旧规则，10月用新规则
  const sep = (await api(owner, 'GET', `/api/room-types/${rt1.id}/rules?at=2026-09-15`)).body;
  const oct = (await api(owner, 'GET', `/api/room-types/${rt1.id}/rules?at=2026-10-15`)).body;
  assert.equal(sep[0].text, '15:00 后入住');
  assert.equal(oct[0].text, '14:00 后入住');
});

// ---------- 3. 两人调整同一镜头 ----------
test('两人同时调整同一镜头：后到者 409 并拿到最新版本', async () => {
  const { proj } = await scaffold();
  const shot = (await api(owner, 'POST', `/api/projects/${proj.id}/shots`, { name: '开场航拍', duration: 6 })).body;

  // 阿宁基于 v1 修改成功
  const r1 = await api(owner, 'PATCH', `/api/shots/${shot.id}`, { base_version: 1, duration: 8 });
  assert.equal(r1.status, 200);
  assert.equal(r1.body.version, 2);

  // 小周仍基于 v1 修改 → 冲突，返回当前版本供合并
  const r2 = await api(editor, 'PATCH', `/api/shots/${shot.id}`, { base_version: 1, duration: 10 });
  assert.equal(r2.status, 409);
  assert.equal(r2.body.current.version, 2);
  assert.equal(r2.body.current.duration, 8);

  // 小周基于最新 v2 重试 → 成功
  const r3 = await api(editor, 'PATCH', `/api/shots/${shot.id}`, { base_version: 2, duration: 10 });
  assert.equal(r3.status, 200);
  assert.equal(r3.body.updated_by, editor);
});

// ---------- 4. 上传重试与恢复 ----------
test('分块上传：会话与块级幂等，重试不产生副作用', async () => {
  const { proj } = await scaffold();
  // 创建会话（网络重试同 key → 返回同一会话）
  const s1 = (await api(editor, 'POST', '/api/uploads', { project_id: proj.id, filename: 'room.mp4', total_chunks: 3, idempotency_key: 'up-001' })).body;
  const s2 = await api(editor, 'POST', '/api/uploads', { project_id: proj.id, filename: 'room.mp4', total_chunks: 3, idempotency_key: 'up-001' });
  assert.equal(s2.body.id, s1.id);
  assert.equal(s2.body.deduplicated, true);

  // 上传块0（重试同 key → 幂等；换 key → 409）
  const c1 = await api(editor, 'PUT', `/api/uploads/${s1.id}/chunks/0`, { data: 'bin' }, { 'x-chunk-key': 'ck-0' });
  assert.equal(c1.status, 200);
  const c1retry = await api(editor, 'PUT', `/api/uploads/${s1.id}/chunks/0`, { data: 'bin' }, { 'x-chunk-key': 'ck-0' });
  assert.equal(c1retry.body.deduplicated, true);
  const c1conflict = await api(editor, 'PUT', `/api/uploads/${s1.id}/chunks/0`, { data: 'bin' }, { 'x-chunk-key': 'ck-OTHER' });
  assert.equal(c1conflict.status, 409);

  // 缺块时 complete → 409 并列出缺失块
  const inc = await api(editor, 'POST', `/api/uploads/${s1.id}/complete`);
  assert.equal(inc.status, 409);
  assert.deepEqual(inc.body.missing, [1, 2]);

  await api(editor, 'PUT', `/api/uploads/${s1.id}/chunks/1`, {}, { 'x-chunk-key': 'ck-1' });
  await api(editor, 'PUT', `/api/uploads/${s1.id}/chunks/2`, {}, { 'x-chunk-key': 'ck-2' });
  const done = await api(editor, 'POST', `/api/uploads/${s1.id}/complete`);
  assert.equal(done.body.status, 'completed');
  // 重复 complete 幂等
  const done2 = await api(editor, 'POST', `/api/uploads/${s1.id}/complete`);
  assert.equal(done2.body.deduplicated, true);
});

// ---------- 5. 恢复上次项目时权限改变 ----------
test('恢复上次项目：权限被收回后返回 403', async () => {
  const { proj } = await scaffold();
  // 小周打开过项目 → 会话记录
  const open = await api(editor, 'GET', `/api/projects/${proj.id}`);
  assert.equal(open.status, 200);
  const resumed1 = await api(editor, 'GET', '/api/session/last-project');
  assert.equal(resumed1.status, 200);
  assert.equal(resumed1.body.project.id, proj.id);

  // 房东收回小周权限
  await api(owner, 'POST', `/api/projects/${proj.id}/acl`, { user_id: editor, role: null });
  const resumed2 = await api(editor, 'GET', '/api/session/last-project');
  assert.equal(resumed2.status, 403);
  assert.match(resumed2.body.detail, /权限已被收回/);

  // 降级为 viewer 后可恢复但只读
  await api(owner, 'POST', `/api/projects/${proj.id}/acl`, { user_id: editor, role: 'viewer' });
  const resumed3 = await api(editor, 'GET', '/api/session/last-project');
  assert.equal(resumed3.body.role, 'viewer');
  const edit = await api(editor, 'POST', `/api/projects/${proj.id}/shots`, { name: 'x' });
  assert.equal(edit.status, 403);
});

// ---------- 7. 渠道级安全区检查 ----------
test('竖版裁切可能遮掉必要提示：渠道级安全区检查', async () => {
  const { proj } = await scaffold();
  // 横版 16:9 渠道 + 竖版 9:16 渠道
  const chH = (await api(owner, 'POST', '/api/channels', { name: '横版-OTA', aspect_ratio: '16:9', safe_area: { top: 5, right: 5, bottom: 5, left: 5 } })).body;
  const chV = (await api(owner, 'POST', '/api/channels', { name: '竖版-短视频', aspect_ratio: '9:16', safe_area: { top: 12, right: 8, bottom: 15, left: 8 } })).body;
  await api(owner, 'POST', `/api/projects/${proj.id}/channels/${chH.id}/bind`);
  await api(owner, 'POST', `/api/projects/${proj.id}/channels/${chV.id}/bind`);

  // 必要提示放在母版左边缘（横版安全，竖版会被裁掉）
  await api(owner, 'POST', `/api/projects/${proj.id}/shots`, {
    name: '优惠口播', duration: 5,
    overlays: [{ type: 'notice', text: '不可退订', required: true, box: { x: 0.06, y: 0.8, w: 0.15, h: 0.08 } }],
  });

  // 横版检查通过
  const j1 = (await api(owner, 'POST', `/api/projects/${proj.id}/channels/${chH.id}/check`)).body;
  const r1 = await waitJob(j1.job_id);
  assert.equal(r1.result.ok, true);

  // 竖版检查失败：必要提示被裁切
  const j2 = (await api(owner, 'POST', `/api/projects/${proj.id}/channels/${chV.id}/check`)).body;
  const r2 = await waitJob(j2.job_id);
  assert.equal(r2.result.ok, false);
  assert.equal(r2.result.violations[0].reason, 'cropped_by_aspect');
  assert.match(r2.result.violations[0].detail, /不可退订/);

  // 发布被竖版违规拦截
  const pub = await api(owner, 'POST', `/api/projects/${proj.id}/publish`);
  assert.equal(pub.status, 422);
  assert.ok(pub.body.problems.some((p) => p.type === 'safe_area_violation'));
});

// ---------- 10. 引用 vs 复制 ----------
test('引用vs复制：母素材更新、授权撤回、历史复现', async () => {
  const { prop, proj } = await scaffold();
  const mat = (await api(owner, 'POST', `/api/properties/${prop.id}/materials`, {
    title: '客厅视频', scope: 'property_shared', tags: ['living'], content_hash: 'v1hash',
  })).body;

  // 引用 + 复制 两种方式各加一个 clip
  const refClip = (await api(owner, 'POST', `/api/projects/${proj.id}/clips`, { material_id: mat.id, source_mode: 'reference' })).body;
  const copyClip = (await api(owner, 'POST', `/api/projects/${proj.id}/clips`, { material_id: mat.id, source_mode: 'copy' })).body;
  assert.equal(refClip.material_version, 1);
  assert.equal(copyClip.copied_snapshot.content_hash, 'v1hash');

  // 母素材更新 → v2：引用 clip 变 stale，复制 clip 不受影响
  await api(owner, 'PATCH', `/api/materials/${mat.id}`, { content_hash: 'v2hash' });
  const detail = (await api(owner, 'GET', `/api/projects/${proj.id}`)).body;
  const refState = detail.clips.find((c) => c.id === refClip.id);
  const copyState = detail.clips.find((c) => c.id === copyClip.id);
  assert.equal(refState.freshness, 'stale');
  assert.equal(copyState.freshness, 'ok');

  // 引用 clip 显式跟随母版
  await api(owner, 'POST', `/api/clips/${refClip.id}/follow-master`);
  const after = (await api(owner, 'GET', `/api/projects/${proj.id}`)).body;
  assert.equal(after.clips.find((c) => c.id === refClip.id).freshness, 'ok');

  // 授权撤回 → 引用与复制都被发布检查拦截（复制不转移授权）
  await api(owner, 'POST', `/api/materials/${mat.id}/revoke-license`);
  const pub = await api(owner, 'POST', `/api/projects/${proj.id}/publish`);
  assert.equal(pub.status, 422);
  const licProblems = pub.body.problems.filter((p) => p.type === 'license_revoked');
  assert.equal(licProblems.length, 2);
  assert.ok(licProblems.some((p) => /复制件仍受母素材授权约束/.test(p.detail)));
});

// ---------- 2. 照片来源失效 ----------
test('照片来源失效：发布拦截，旧导出保留制作时真实信息', async () => {
  const { prop, proj } = await scaffold();
  const photo = (await api(owner, 'POST', `/api/properties/${prop.id}/materials`, {
    title: '房间照片', kind: 'image', scope: 'property_shared', content_hash: 'ph1',
  })).body;
  await api(owner, 'POST', `/api/projects/${proj.id}/shots`, { name: '房间展示', duration: 4 });
  await api(owner, 'POST', `/api/projects/${proj.id}/clips`, { material_id: photo.id, source_mode: 'reference' });

  // 先发布一次（素材健康）→ 导出快照
  const pub1 = await api(owner, 'POST', `/api/projects/${proj.id}/publish`);
  assert.equal(pub1.status, 201);
  const exp1 = pub1.body.exports[0];
  assert.equal(exp1.materials[0].license_status_at_production, 'active');

  // 照片来源失效 → 再次发布被拦截
  await api(owner, 'POST', `/api/materials/${photo.id}/invalidate-source`);
  const pub2 = await api(owner, 'POST', `/api/projects/${proj.id}/publish`);
  assert.equal(pub2.status, 422);
  assert.ok(pub2.body.problems.some((p) => p.type === 'source_missing'));

  // 旧导出仍是制作时的真实状态（active），不被新状态污染
  const expRead = (await api(owner, 'GET', `/api/exports/${exp1.id}`)).body;
  assert.equal(expRead.materials[0].license_status_at_production, 'active');
});

// ---------- 9. 旧导出不套用当前房型描述；无来源周边不推荐 ----------
test('导出快照：房型描述更新不回写旧导出；无依据周边不生成', async () => {
  const { rt1, proj } = await scaffold();
  await api(owner, 'POST', `/api/projects/${proj.id}/shots`, { name: 's1', duration: 3 });

  // 项目里塞两条周边推荐：一条有来源，一条无依据
  store.update('projects', proj.id, {
    nearby_recommendations: [
      { text: '步行5分钟到地铁站', source: { url: 'https://map.example.com/poi/1' } },
      { text: '附近有网红咖啡馆' }, // 无来源
    ],
  });

  const pub = await api(owner, 'POST', `/api/projects/${proj.id}/publish`);
  assert.equal(pub.status, 201);
  const exp = pub.body.exports[0];
  assert.equal(exp.room_type_descriptions[rt1.id].description, '带阳台，看日出');
  assert.equal(exp.nearby_recommendations.length, 1);
  assert.equal(exp.nearby_dropped_for_no_source, 1);

  // 房型描述更新后，旧导出保持原样
  await api(owner, 'PATCH', `/api/room-types/${rt1.id}`, { description: '带阳台，看日出（已翻新）' });
  const expRead = (await api(owner, 'GET', `/api/exports/${exp.id}`)).body;
  assert.equal(expRead.room_type_descriptions[rt1.id].description, '带阳台，看日出');
  assert.equal(expRead.room_type_descriptions[rt1.id].description_version, 1);
});

// ---------- 8. 首页游标 + 发布前重检 ----------
test('首页游标引用项目版本；发布前重新检查素材与规则依赖', async () => {
  const { rt1, proj } = await scaffold();
  // 规则：字幕引用了规则，发布后规则被修订 → 旧引用失效 → 再发布被拦
  const rule = (await api(owner, 'POST', `/api/room-types/${rt1.id}/rules`, {
    title: '押金', text: '押金 500 元', effective_from: '2026-01-01',
  })).body;
  await api(owner, 'POST', `/api/projects/${proj.id}/shots`, {
    name: '规则口播', duration: 4,
    overlays: [{ type: 'notice', text: '押金 500 元', required: true, rule_id: rule.id, box: { x: 0.4, y: 0.8, w: 0.2, h: 0.08 } }],
  });
  const ver = (await api(owner, 'POST', `/api/projects/${proj.id}/versions`)).body;

  // 设置首页游标 → 引用该版本
  const cur = await api(owner, 'PUT', '/api/homepage/cursor', { project_id: proj.id, version_id: ver.id });
  assert.equal(cur.status, 201);
  const home1 = (await api(owner, 'GET', '/api/homepage')).body;
  assert.equal(home1[0].version_no, ver.version_no);
  assert.equal(home1[0].dependency_ok, true);

  // 规则被修订（旧规则 10 月起失效），字幕仍引用旧规则 → 依赖不再健康
  await api(owner, 'POST', `/api/room-types/${rt1.id}/rules`, {
    title: '押金', text: '免押金', effective_from: '2026-10-01', revises: rule.id,
  });
  const home2 = (await api(owner, 'GET', '/api/homepage')).body;
  assert.equal(home2[0].dependency_ok, false);
  assert.ok(home2[0].dependency_problems.some((p) => p.type === 'rule_expired'));

  // 发布（as_of 10月）被过期规则拦截
  const pub = await api(owner, 'POST', `/api/projects/${proj.id}/publish`, { as_of: '2026-10-03' });
  assert.equal(pub.status, 422);
  assert.ok(pub.body.problems.some((p) => p.type === 'rule_expired'));
});

// ---------- 媒体工作器产物 ----------
test('媒体工作器输出字幕、镜头清单、预览', async () => {
  const { proj } = await scaffold();
  await api(owner, 'POST', `/api/projects/${proj.id}/shots`, {
    name: '开场', duration: 4,
    overlays: [{ type: 'subtitle', text: '欢迎来到山景民宿', box: { x: 0.3, y: 0.85, w: 0.4, h: 0.08 } }],
  });
  await api(owner, 'POST', `/api/projects/${proj.id}/shots`, {
    name: '房间', duration: 6,
    overlays: [{ type: 'subtitle', text: '山景大床房', box: { x: 0.3, y: 0.85, w: 0.4, h: 0.08 } }],
  });

  const sub = await waitJob((await api(owner, 'POST', `/api/projects/${proj.id}/jobs/render_subtitles`)).body.job_id);
  assert.equal(sub.result.format, 'srt');
  assert.equal(sub.result.cue_count, 2);
  assert.match(sub.result.content, /00:00:00,000 --> 00:00:04,000/);
  assert.match(sub.result.content, /山景大床房/);

  const list = await waitJob((await api(owner, 'POST', `/api/projects/${proj.id}/jobs/shot_list`)).body.job_id);
  assert.equal(list.result.count, 2);
  assert.equal(list.result.items[1].name, '房间');

  const prev = await waitJob((await api(owner, 'POST', `/api/projects/${proj.id}/jobs/preview`)).body.job_id);
  assert.match(prev.result.url, /\/previews\//);
});

// ---------- 脚本变更后安全区结论不可复用 ----------
test('脚本变更后必须重新做渠道检查，不能复用旧结论发布', async () => {
  const { proj } = await scaffold();
  const chV = (await api(owner, 'POST', '/api/channels', { name: '竖版', aspect_ratio: '9:16', safe_area: { top: 10, right: 10, bottom: 10, left: 10 } })).body;
  await api(owner, 'POST', `/api/projects/${proj.id}/channels/${chV.id}/bind`);
  const shot = (await api(owner, 'POST', `/api/projects/${proj.id}/shots`, {
    name: '口播', duration: 5,
    overlays: [{ type: 'notice', text: '免费取消', required: true, box: { x: 0.42, y: 0.5, w: 0.16, h: 0.08 } }],
  })).body;
  const j = (await api(owner, 'POST', `/api/projects/${proj.id}/channels/${chV.id}/check`)).body;
  assert.equal((await waitJob(j.job_id)).result.ok, true);

  // 修改镜头（脚本版本变化）→ 旧检查结论失效 → 发布被拦
  await api(owner, 'PATCH', `/api/shots/${shot.id}`, { base_version: 1, duration: 7 });
  const pub = await api(owner, 'POST', `/api/projects/${proj.id}/publish`);
  assert.equal(pub.status, 422);
  assert.ok(pub.body.problems.some((p) => p.type === 'safe_area_unchecked'));

  // 重新检查后即可发布
  const j2 = (await api(owner, 'POST', `/api/projects/${proj.id}/channels/${chV.id}/check`)).body;
  assert.equal((await waitJob(j2.job_id)).result.ok, true);
  const pub2 = await api(owner, 'POST', `/api/projects/${proj.id}/publish`);
  assert.equal(pub2.status, 201);
});

after(() => { if (server) server.close(); });
