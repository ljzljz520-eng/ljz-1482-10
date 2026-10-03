'use strict';
const express = require('express');
const domain = require('./domain');
const { authMiddleware, requireProjectRole, projectRole } = require('./auth');

function createApp(store, worker) {
  const app = express();
  app.use(express.json({ limit: '10mb' }));

  // 种子用户（演示/测试引导，免认证）
  app.post('/api/dev/seed-users', (req, res) => {
    if (store.data.users.length) return res.json(store.data.users);
    const mk = (name) => store.insert('users', { id: store.nextId('user'), name });
    res.json([mk('阿宁(owner)'), mk('小周(editor)'), mk('老陈(viewer)')]);
  });

  app.use('/api', authMiddleware(store));

  const wrap = (fn) => (req, res) => {
    try { fn(req, res); } catch (e) {
      res.status(e.status || 500).json({ error: e.message });
    }
  };
  const bad = (msg, status = 400) => { const e = new Error(msg); e.status = status; throw e; };

  // ============ 房源 / 房型 ============
  app.post('/api/properties', wrap((req, res) => {
    const { name, address = '' } = req.body;
    if (!name) bad('name 必填');
    const p = store.insert('properties', {
      id: store.nextId('prop'), name, address,
      created_at: new Date().toISOString(),
    });
    res.status(201).json(p);
  }));

  app.get('/api/properties', wrap((req, res) => res.json(store.data.properties)));

  app.post('/api/properties/:id/room-types', wrap((req, res) => {
    const prop = store.get('properties', req.params.id);
    if (!prop) bad('房源不存在', 404);
    const { name, description = '' } = req.body;
    if (!name) bad('name 必填');
    const rt = store.insert('room_types', {
      id: store.nextId('rt'), property_id: prop.id, name, description,
      description_version: 1, created_at: new Date().toISOString(),
    });
    res.status(201).json(rt);
  }));

  app.get('/api/properties/:id/room-types', wrap((req, res) =>
    res.json(store.find('room_types', (r) => r.property_id === req.params.id))));

  /** 更新房型描述：版本+1；已导出的快照不回写 */
  app.patch('/api/room-types/:id', wrap((req, res) => {
    const rt = store.get('room_types', req.params.id);
    if (!rt) bad('房型不存在', 404);
    const patch = {};
    if (req.body.name !== undefined) patch.name = req.body.name;
    if (req.body.description !== undefined) {
      patch.description = req.body.description;
      patch.description_version = rt.description_version + 1;
    }
    res.json(store.update('room_types', rt.id, patch));
  }));

  // ============ 设施 / 窗景 / 规则（绑定房型 + 有效日期） ============
  function bindEffective(coll, fields) {
    return wrap((req, res) => {
      const rt = store.get('room_types', req.params.id);
      if (!rt) bad('房型不存在', 404);
      const { effective_from, effective_to = null } = req.body;
      if (!effective_from) bad('effective_from 必填');
      if (effective_to && effective_to <= effective_from) bad('effective_to 必须晚于 effective_from');
      const row = { id: store.nextId(coll.slice(0, 3)), room_type_id: rt.id, effective_from, effective_to, created_at: new Date().toISOString() };
      for (const f of fields) {
        if (!req.body[f]) bad(`${f} 必填`);
        row[f] = req.body[f];
      }
      res.status(201).json(store.insert(coll, row));
    });
  }
  app.post('/api/room-types/:id/facilities', bindEffective('facilities', ['name']));
  app.post('/api/room-types/:id/window-views', bindEffective('window_views', ['view_type', 'description']));
  app.get('/api/room-types/:id/facilities', wrap((req, res) => {
    const at = req.query.at || '9999-12-31';
    res.json(domain.effectiveOn(store.data.facilities, req.params.id, at));
  }));
  app.get('/api/room-types/:id/window-views', wrap((req, res) => {
    const at = req.query.at || '9999-12-31';
    res.json(domain.effectiveOn(store.data.window_views, req.params.id, at));
  }));

  /** 规则：新建或修订（revises 指向旧规则） */
  app.post('/api/room-types/:id/rules', wrap((req, res) => {
    const rt = store.get('room_types', req.params.id);
    if (!rt) bad('房型不存在', 404);
    const rule = domain.reviseRule(store, { ...req.body, room_type_id: rt.id, by: req.user.id });
    res.status(201).json(rule);
  }));
  app.get('/api/room-types/:id/rules', wrap((req, res) => {
    const at = req.query.at;
    const all = store.find('rules', (r) => r.room_type_id === req.params.id);
    res.json(at ? domain.effectiveOn(all, req.params.id, at) : all);
  }));

  // ============ 素材库 ============
  app.post('/api/properties/:id/materials', wrap((req, res) => {
    const prop = store.get('properties', req.params.id);
    if (!prop) bad('房源不存在', 404);
    const { title, kind = 'video', scope, room_type_id = null, tags = [],
            space_label = 'shared', content_hash = null, license = {} } = req.body;
    if (!title) bad('title 必填');
    if (!['property_shared', 'room_private'].includes(scope)) bad('scope 须为 property_shared|room_private');
    if (scope === 'room_private') {
      const rt = room_type_id && store.get('room_types', room_type_id);
      if (!rt || rt.property_id !== prop.id) bad('私有素材必须绑定本房源的房型');
    }
    if (scope === 'property_shared' && room_type_id) bad('公共素材不能绑定单一房型');
    const m = store.insert('materials', {
      id: store.nextId('mat'), property_id: prop.id, title, kind, scope,
      room_type_id: scope === 'room_private' ? room_type_id : null,
      tags, space_label,
      status: 'active', // active | source_missing
      version: 1,
      content_hash: content_hash || `h_${Date.now()}`,
      license: {
        status: license.status || 'active', // active | revoked
        holder: license.holder || req.user.id,
        expires_at: license.expires_at || null,
      },
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    });
    const v = domain.validateSpaceLabel(m, space_label, room_type_id);
    if (!v.ok) { store.remove('materials', m.id); bad(v.reason); }
    res.status(201).json(m);
  }));

  app.get('/api/properties/:id/materials', wrap((req, res) =>
    res.json(store.find('materials', (m) => m.property_id === req.params.id))));

  /** 母素材更新：version+1，引用它的 clip 读取时呈现 stale（不静默替换） */
  app.patch('/api/materials/:id', wrap((req, res) => {
    const m = store.get('materials', req.params.id);
    if (!m) bad('素材不存在', 404);
    const patch = { updated_at: new Date().toISOString() };
    if (req.body.title !== undefined) patch.title = req.body.title;
    if (req.body.content_hash !== undefined && req.body.content_hash !== m.content_hash) {
      patch.content_hash = req.body.content_hash;
      patch.version = m.version + 1; // 内容变化 → 母版版本递增
    }
    const updated = store.update('materials', m.id, patch);
    const refs = store.find('clips', (c) => c.material_id === m.id && c.source_mode === 'reference');
    res.json({ material: updated, affected_reference_clips: refs.map((c) => c.id) });
  }));

  /** 空间标注：大厅等公共素材不能被标成每间房的私有空间 */
  app.post('/api/materials/:id/space-label', wrap((req, res) => {
    const m = store.get('materials', req.params.id);
    if (!m) bad('素材不存在', 404);
    const { space_label, room_type_id = null } = req.body;
    const v = domain.validateSpaceLabel(m, space_label, room_type_id);
    if (!v.ok) return res.status(409).json({ error: v.reason, material_id: m.id });
    res.json(store.update('materials', m.id, { space_label, room_type_id: space_label === 'private' ? (room_type_id || m.room_type_id) : null }));
  }));

  /** 授权撤回：引用与复制件在发布检查时都会被拦截 */
  app.post('/api/materials/:id/revoke-license', wrap((req, res) => {
    const m = store.get('materials', req.params.id);
    if (!m) bad('素材不存在', 404);
    res.json(store.update('materials', m.id, { license: { ...m.license, status: 'revoked', revoked_at: new Date().toISOString() } }));
  }));

  /** 照片来源失效（原图被删除/链接失效） */
  app.post('/api/materials/:id/invalidate-source', wrap((req, res) => {
    const m = store.get('materials', req.params.id);
    if (!m) bad('素材不存在', 404);
    res.json(store.update('materials', m.id, { status: 'source_missing' }));
  }));

  // ============ 项目 / 镜头 / 时间线 ============
  app.post('/api/projects', wrap((req, res) => {
    const { property_id, name } = req.body;
    if (!store.get('properties', property_id)) bad('房源不存在', 404);
    if (!name) bad('name 必填');
    const p = store.insert('projects', {
      id: store.nextId('proj'), property_id, name, owner_id: req.user.id,
      status: 'draft', script_version: 1,
      nearby_recommendations: [],
      created_at: new Date().toISOString(),
    });
    store.insert('acls', { id: store.nextId('acl'), project_id: p.id, user_id: req.user.id, role: 'owner' });
    // 记录会话游标：恢复上次项目用
    upsertSession(req.user.id, p.id);
    res.status(201).json(p);
  }));

  function upsertSession(userId, projectId) {
    const s = store.findOne('sessions', (x) => x.user_id === userId);
    if (s) store.update('sessions', s.id, { last_project_id: projectId, updated_at: new Date().toISOString() });
    else store.insert('sessions', { id: store.nextId('sess'), user_id: userId, last_project_id: projectId, updated_at: new Date().toISOString() });
  }

  app.get('/api/projects', wrap((req, res) => {
    const visible = store.data.projects.filter((p) => projectRole(store, p.id, req.user.id));
    res.json(visible);
  }));

  app.get('/api/projects/:id', requireProjectRole(store, 'viewer'), wrap((req, res) => {
    upsertSession(req.user.id, req.project.id);
    const shots = store.find('shots', (s) => s.project_id === req.project.id).sort((a, b) => a.idx - b.idx);
    const clips = store.find('clips', (c) => c.project_id === req.project.id)
      .map((c) => ({ ...c, freshness: domain.clipFreshness(store, c).status }));
    res.json({ ...req.project, role: req.projectRole, shots, clips });
  }));

  /** 项目 ACL 管理（演示权限改变场景） */
  app.post('/api/projects/:id/acl', requireProjectRole(store, 'owner'), wrap((req, res) => {
    const { user_id, role } = req.body; // role: editor|viewer|null(移除)
    if (!store.get('users', user_id)) bad('用户不存在', 404);
    const existing = store.findOne('acls', (a) => a.project_id === req.project.id && a.user_id === user_id);
    if (role === null) {
      if (existing) store.remove('acls', existing.id);
      return res.json({ ok: true, removed: user_id });
    }
    if (!['editor', 'viewer', 'owner'].includes(role)) bad('role 非法');
    if (existing) return res.json(store.update('acls', existing.id, { role }));
    res.status(201).json(store.insert('acls', { id: store.nextId('acl'), project_id: req.project.id, user_id, role }));
  }));

  /** 恢复上次项目：重新实时校验权限（权限可能在上次会话后被改变） */
  app.get('/api/session/last-project', wrap((req, res) => {
    const s = store.findOne('sessions', (x) => x.user_id === req.user.id);
    if (!s || !s.last_project_id) return res.status(404).json({ error: '无历史项目' });
    const role = projectRole(store, s.last_project_id, req.user.id);
    if (!role) return res.status(403).json({ error: 'forbidden', detail: '上次项目权限已被收回', project_id: s.last_project_id });
    const project = store.get('projects', s.last_project_id);
    res.json({ project, role });
  }));

  // ---- 镜头：乐观锁，两人同时调整同一镜头时后到者 409 ----
  app.post('/api/projects/:id/shots', requireProjectRole(store, 'editor'), wrap((req, res) => {
    const { name, idx = null, duration = 5, overlays = [] } = req.body;
    if (!name) bad('name 必填');
    const count = store.find('shots', (s) => s.project_id === req.project.id).length;
    const shot = store.insert('shots', {
      id: store.nextId('shot'), project_id: req.project.id,
      idx: idx == null ? count : idx, name, duration,
      overlays: overlays.map((o, i) => ({ id: `ov_${Date.now()}_${i}`, required: false, ...o })),
      version: 1, updated_by: req.user.id, updated_at: new Date().toISOString(),
    });
    bumpScript(req.project.id);
    res.status(201).json(shot);
  }));

  function bumpScript(projectId) {
    const p = store.get('projects', projectId);
    store.update('projects', projectId, { script_version: p.script_version + 1 });
  }

  app.patch('/api/shots/:id', wrap((req, res) => {
    const shot = store.get('shots', req.params.id);
    if (!shot) bad('镜头不存在', 404);
    const role = projectRole(store, shot.project_id, req.user.id);
    if (!role || role === 'viewer') return res.status(403).json({ error: 'forbidden' });
    const { base_version } = req.body;
    if (base_version == null) bad('base_version 必填（乐观锁）');
    if (shot.version !== base_version) {
      // 冲突：返回当前最新，供调用方合并
      return res.status(409).json({ error: 'version_conflict', current: shot });
    }
    const patch = { version: shot.version + 1, updated_by: req.user.id, updated_at: new Date().toISOString() };
    if (req.body.name !== undefined) patch.name = req.body.name;
    if (req.body.duration !== undefined) patch.duration = req.body.duration;
    if (req.body.overlays !== undefined) patch.overlays = req.body.overlays;
    const updated = store.update('shots', shot.id, patch);
    bumpScript(shot.project_id);
    res.json(updated);
  }));

  // ---- 时间线片段：引用 vs 复制 ----
  app.post('/api/projects/:id/clips', requireProjectRole(store, 'editor'), wrap((req, res) => {
    const clip = domain.makeClip(store, { ...req.body, project_id: req.project.id, by: req.user.id });
    bumpScript(req.project.id);
    res.status(201).json(clip);
  }));

  /** 引用 clip 跟随母版更新（显式操作，非静默） */
  app.post('/api/clips/:id/follow-master', wrap((req, res) => {
    const clip = store.get('clips', req.params.id);
    if (!clip) bad('clip 不存在', 404);
    if (clip.source_mode !== 'reference') bad('仅引用型 clip 可跟随母版');
    const m = store.get('materials', clip.material_id);
    if (!m || m.license.status !== 'active') bad('母素材不可用', 409);
    res.json(store.update('clips', clip.id, { material_version: m.version }));
  }));

  // ---- 项目版本快照 ----
  app.post('/api/projects/:id/versions', requireProjectRole(store, 'editor'), wrap((req, res) => {
    const shots = store.find('shots', (s) => s.project_id === req.project.id);
    const clips = store.find('clips', (c) => c.project_id === req.project.id);
    const no = store.find('project_versions', (v) => v.project_id === req.project.id).length + 1;
    const v = store.insert('project_versions', {
      id: store.nextId('ver'), project_id: req.project.id, version_no: no,
      snapshot: { shots, clips, script_version: req.project.script_version },
      created_by: req.user.id, created_at: new Date().toISOString(),
    });
    res.status(201).json(v);
  }));

  app.get('/api/projects/:id/versions', requireProjectRole(store, 'viewer'), wrap((req, res) =>
    res.json(store.find('project_versions', (v) => v.project_id === req.project.id))));

  // ============ 渠道与安全区 ============
  app.post('/api/channels', wrap((req, res) => {
    const { name, aspect_ratio, safe_area } = req.body;
    if (!name || !aspect_ratio || !safe_area) bad('name/aspect_ratio/safe_area 必填');
    res.status(201).json(store.insert('channels', { id: store.nextId('ch'), name, aspect_ratio, safe_area }));
  }));
  app.get('/api/channels', wrap((req, res) => res.json(store.data.channels)));

  app.post('/api/projects/:id/channels/:channelId/bind', requireProjectRole(store, 'editor'), wrap((req, res) => {
    if (!store.get('channels', req.params.channelId)) bad('渠道不存在', 404);
    const existing = store.findOne('project_channels', (pc) => pc.project_id === req.project.id && pc.channel_id === req.params.channelId);
    if (existing) return res.json(existing);
    res.status(201).json(store.insert('project_channels', { id: store.nextId('pc'), project_id: req.project.id, channel_id: req.params.channelId }));
  }));

  app.get('/api/projects/:id/channels', requireProjectRole(store, 'viewer'), wrap((req, res) => {
    res.json(store.find('project_channels', (pc) => pc.project_id === req.project.id));
  }));

  /** 触发渠道级安全区检查（媒体工作器执行，按当前脚本版本） */
  app.post('/api/projects/:id/channels/:channelId/check', requireProjectRole(store, 'editor'), wrap((req, res) => {
    const job = worker.submit('safe_area_check', { project_id: req.project.id, channel_id: req.params.channelId });
    res.status(202).json({ job_id: job.id });
  }));

  app.get('/api/projects/:id/channels/:channelId/check', requireProjectRole(store, 'viewer'), wrap((req, res) => {
    const checks = store.find('safe_area_checks', (c) => c.project_id === req.project.id && c.channel_id === req.params.channelId);
    res.json(checks.sort((a, b) => (a.created_at < b.created_at ? 1 : -1)));
  }));

  // ============ 媒体工作器任务 ============
  app.post('/api/projects/:id/jobs/:type', requireProjectRole(store, 'editor'), wrap((req, res) => {
    if (!['render_subtitles', 'shot_list', 'preview'].includes(req.params.type)) bad('未知任务类型');
    const job = worker.submit(req.params.type, { project_id: req.project.id });
    res.status(202).json({ job_id: job.id });
  }));
  app.get('/api/jobs/:id', wrap((req, res) => {
    const job = store.get('jobs', req.params.id);
    if (!job) bad('任务不存在', 404);
    res.json(job);
  }));

  // ============ 首页游标 ============
  /** 游标引用「项目 + 版本」，不是裸项目——首页展示固定在指定版本 */
  app.put('/api/homepage/cursor', wrap((req, res) => {
    const { project_id, version_id } = req.body;
    const project = store.get('projects', project_id);
    if (!project) bad('项目不存在', 404);
    const version = store.get('project_versions', version_id);
    if (!version || version.project_id !== project_id) bad('版本不属于该项目');
    const existing = store.findOne('homepage_cursors', (c) => c.project_id === project_id);
    const row = { project_id, version_id, set_by: req.user.id, set_at: new Date().toISOString() };
    if (existing) return res.json(store.update('homepage_cursors', existing.id, row));
    res.status(201).json(store.insert('homepage_cursors', { id: store.nextId('cur'), ...row }));
  }));

  /** 首页读取：解析游标对应版本，并实时给出依赖健康度（不阻塞展示，仅提示） */
  app.get('/api/homepage', wrap((req, res) => {
    const cursors = store.data.homepage_cursors.map((c) => {
      const version = store.get('project_versions', c.version_id);
      const project = store.get('projects', c.project_id);
      const dep = domain.publishCheck(store, project, new Date().toISOString().slice(0, 10));
      return { ...c, version_no: version ? version.version_no : null, project_name: project ? project.name : null, dependency_ok: dep.ok, dependency_problems: dep.problems };
    });
    res.json(cursors);
  }));

  // ============ 发布与导出 ============
  /** 发布：强制重新检查素材授权/来源、规则有效性、渠道安全区 */
  app.post('/api/projects/:id/publish', requireProjectRole(store, 'editor'), wrap((req, res) => {
    const asOf = req.body.as_of || new Date().toISOString().slice(0, 10);
    const check = domain.publishCheck(store, req.project, asOf);
    if (!check.ok) return res.status(422).json({ error: 'publish_blocked', problems: check.problems });
    // 发布即产生一个版本 + 各绑定渠道的不可变导出
    const shots = store.find('shots', (s) => s.project_id === req.project.id);
    const clips = store.find('clips', (c) => c.project_id === req.project.id);
    const no = store.find('project_versions', (v) => v.project_id === req.project.id).length + 1;
    const version = store.insert('project_versions', {
      id: store.nextId('ver'), project_id: req.project.id, version_no: no,
      snapshot: { shots, clips, script_version: req.project.script_version },
      created_by: req.user.id, created_at: new Date().toISOString(),
    });
    const bindings = store.find('project_channels', (pc) => pc.project_id === req.project.id);
    const targets = bindings.length ? bindings.map((b) => store.get('channels', b.channel_id)) : [null];
    const exportsMade = targets.map((ch) => store.insert('exports', {
      id: store.nextId('exp'),
      ...domain.buildExportSnapshot(store, req.project, version, ch, req.user.id),
    }));
    store.update('projects', req.project.id, { status: 'published', published_at: new Date().toISOString() });
    res.status(201).json({ version, exports: exportsMade, check });
  }));

  /** 导出只读：永远返回制作时快照，不套用当前房型描述 */
  app.get('/api/exports/:id', wrap((req, res) => {
    const exp = store.get('exports', req.params.id);
    if (!exp) bad('导出不存在', 404);
    res.json(exp);
  }));

  app.get('/api/projects/:id/exports', requireProjectRole(store, 'viewer'), wrap((req, res) =>
    res.json(store.find('exports', (e) => e.project_id === req.project.id))));

  // ============ 分块上传：幂等重试 ============
  app.post('/api/uploads', wrap((req, res) => {
    const { project_id, filename, total_chunks, idempotency_key } = req.body;
    if (!project_id || !filename || !total_chunks || !idempotency_key) bad('project_id/filename/total_chunks/idempotency_key 必填');
    const role = projectRole(store, project_id, req.user.id);
    if (!role || role === 'viewer') return res.status(403).json({ error: 'forbidden' });
    // 幂等：同一 key 重复创建返回原会话（网络重试安全）
    const dup = store.findOne('upload_sessions', (u) => u.idempotency_key === idempotency_key);
    if (dup) return res.status(200).json({ ...dup, deduplicated: true });
    const s = store.insert('upload_sessions', {
      id: store.nextId('upl'), project_id, filename, total_chunks,
      received: [], chunk_keys: {}, status: 'uploading',
      idempotency_key, created_by: req.user.id, created_at: new Date().toISOString(),
    });
    res.status(201).json(s);
  }));

  app.put('/api/uploads/:id/chunks/:n', wrap((req, res) => {
    const s = store.get('upload_sessions', req.params.id);
    if (!s) bad('上传会话不存在', 404);
    if (s.status !== 'uploading') return res.status(409).json({ error: `会话状态 ${s.status}` });
    const n = Number(req.params.n);
    const key = req.get('x-chunk-key');
    if (!key) bad('x-chunk-key 头必填（块级幂等）');
    if (n < 0 || n >= s.total_chunks) bad('块序号越界');
    if (s.received.includes(n)) {
      // 重试同一块：key 相同 → 幂等成功；不同 → 冲突
      if (s.chunk_keys[n] === key) return res.status(200).json({ ok: true, deduplicated: true, received: s.received });
      return res.status(409).json({ error: 'chunk_key_conflict', chunk: n });
    }
    s.received.push(n);
    s.received.sort((a, b) => a - b);
    s.chunk_keys[n] = key;
    store.update('upload_sessions', s.id, { received: s.received, chunk_keys: s.chunk_keys });
    res.json({ ok: true, received: s.received });
  }));

  app.post('/api/uploads/:id/complete', wrap((req, res) => {
    const s = store.get('upload_sessions', req.params.id);
    if (!s) bad('上传会话不存在', 404);
    if (s.status === 'completed') return res.json({ ...s, deduplicated: true });
    if (s.received.length !== s.total_chunks) {
      return res.status(409).json({ error: 'chunks_incomplete', missing: Array.from({ length: s.total_chunks }, (_, i) => i).filter((i) => !s.received.includes(i)) });
    }
    store.update('upload_sessions', s.id, { status: 'completed', completed_at: new Date().toISOString() });
    res.json(store.get('upload_sessions', s.id));
  }));

  app.get('/api/uploads/:id', wrap((req, res) => {
    const s = store.get('upload_sessions', req.params.id);
    if (!s) bad('上传会话不存在', 404);
    res.json(s);
  }));

  return app;
}

module.exports = { createApp };
