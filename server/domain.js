'use strict';
/**
 * 领域逻辑：房源/房型/素材作用域、规则有效期、引用vs复制、
 * 渠道安全区裁切、发布依赖检查、导出快照。
 */

const AUTHORING_ASPECT = 16 / 9; // 脚本母版统一按 16:9 编写

// ---------- 规则 / 设施 / 窗景：绑定房型 + 有效日期 ----------
function effectiveOn(rows, roomTypeId, date) {
  return rows.filter(
    (r) =>
      r.room_type_id === roomTypeId &&
      r.effective_from <= date &&
      (r.effective_to == null || r.effective_to > date)
  );
}

/** 修订规则：新修订生效时，自动截断被替代规则的有效期 */
function reviseRule(store, { room_type_id, title, text, effective_from, effective_to = null, revises = null, by }) {
  if (!room_type_id || !title || !text || !effective_from) {
    const e = new Error('room_type_id/title/text/effective_from 必填');
    e.status = 400;
    throw e;
  }
  let revision = 1;
  if (revises) {
    const old = store.get('rules', revises);
    if (!old) { const e = new Error('被修订规则不存在'); e.status = 404; throw e; }
    if (old.room_type_id !== room_type_id) { const e = new Error('不能跨房型修订规则'); e.status = 400; throw e; }
    revision = old.revision + 1;
    // 旧规则在新规则生效时截止，保留历史可追溯
    if (old.effective_to == null || old.effective_to > effective_from) {
      store.update('rules', old.id, { effective_to: effective_from, superseded_by: null });
    }
  }
  const rule = {
    id: store.nextId('rule'),
    room_type_id, title, text,
    revision, effective_from, effective_to,
    supersedes: revises, superseded_by: null,
    created_by: by, created_at: new Date().toISOString(),
  };
  store.insert('rules', rule);
  if (revises) store.update('rules', revises, { superseded_by: rule.id });
  return rule;
}

// ---------- 素材作用域 ----------
const SHARED_SPACE_TAGS = new Set(['lobby', 'facade', 'corridor', 'garden', 'parking', 'shared']);

/**
 * 空间标注校验：公共素材（大厅等）不能被标成某间房的私有空间。
 * 返回 {ok} 或 {ok:false, reason}
 */
function validateSpaceLabel(material, spaceLabel, roomTypeId) {
  if (spaceLabel === 'private') {
    if (material.scope === 'property_shared') {
      return { ok: false, reason: '房源级公共素材不能标注为房间私有空间' };
    }
    if ((material.tags || []).some((t) => SHARED_SPACE_TAGS.has(t))) {
      return { ok: false, reason: `含公共空间标签(${SHARED_SPACE_TAGS.has('lobby') ? 'lobby等' : ''})的素材不能标为私有空间` };
    }
    if (!roomTypeId && !material.room_type_id) {
      return { ok: false, reason: '私有空间素材必须绑定具体房型' };
    }
  }
  return { ok: true };
}

// ---------- 引用 vs 复制 ----------
/**
 * clip.source_mode:
 *  - reference: 引用母素材，记录引用时的 material_version（pin）。
 *               母素材更新 → clip 标记 stale，可选择跟随；授权撤回 → 发布拦截。
 *  - copy:      复制进项目，快照内容；母素材更新不影响；但 origin_material_id
 *               仍追溯授权，授权撤回同样拦截（复制不转移授权）。
 */
function makeClip(store, { project_id, shot_id = null, material_id, source_mode, in_point = 0, out_point = null, by }) {
  const material = store.get('materials', material_id);
  if (!material) { const e = new Error('素材不存在'); e.status = 404; throw e; }
  if (material.license.status !== 'active') { const e = new Error('素材授权不可用，不能加入时间线'); e.status = 409; throw e; }
  if (material.status !== 'active') { const e = new Error('素材来源已失效，不能加入时间线'); e.status = 409; throw e; }
  if (!['reference', 'copy'].includes(source_mode)) { const e = new Error('source_mode 须为 reference|copy'); e.status = 400; throw e; }

  const clip = {
    id: store.nextId('clip'),
    project_id, shot_id, material_id, source_mode,
    material_version: material.version, // 引用时 pin 的版本
    origin_material_id: material.id,    // 复制也追溯母素材授权
    copied_snapshot: source_mode === 'copy'
      ? { title: material.title, content_hash: material.content_hash, version: material.version, copied_at: new Date().toISOString() }
      : null,
    in_point, out_point,
    created_by: by, created_at: new Date().toISOString(),
  };
  store.insert('clips', clip);
  return clip;
}

/** 计算 clip 的时效状态：母素材是否已更新（仅 reference 有意义） */
function clipFreshness(store, clip) {
  const m = store.get('materials', clip.material_id);
  if (!m) return { status: 'material_deleted' };
  if (m.license.status !== 'active') return { status: 'license_revoked', material: m };
  if (m.status !== 'active') return { status: 'source_missing', material: m };
  if (clip.source_mode === 'reference' && clip.material_version !== m.version) {
    return { status: 'stale', pinned: clip.material_version, current: m.version, material: m };
  }
  return { status: 'ok', material: m };
}

// ---------- 渠道安全区 ----------
/**
 * 竖版(9:16)由横版(16:9)母版居中裁切：保持全高，宽度收窄。
 * 返回母版坐标系下的可见窗口 {x, y, w, h}（0~1 归一化）。
 */
function visibleWindow(channelAspect) {
  const [aw, ah] = channelAspect.split(':').map(Number);
  const target = aw / ah;
  if (Math.abs(target - AUTHORING_ASPECT) < 1e-6) return { x: 0, y: 0, w: 1, h: 1 };
  if (target < AUTHORING_ASPECT) {
    // 更窄（竖版）：裁左右
    const w = target / AUTHORING_ASPECT;
    return { x: (1 - w) / 2, y: 0, w, h: 1 };
  }
  // 更宽：裁上下
  const h = AUTHORING_ASPECT / target;
  return { x: 0, y: (1 - h) / 2, w: 1, h };
}

/**
 * 渠道级安全区检查：必要提示(required overlay)必须完整落在
 * 「可见窗口 ∩ 安全区」内。安全区百分比基于渠道画面，映射回母版坐标。
 * 不能仅复用横版缩略图判断竖版可用——必须按渠道重新计算。
 */
function checkSafeArea(overlays, channel) {
  const win = visibleWindow(channel.aspect_ratio);
  const sa = channel.safe_area; // {top,right,bottom,left} 渠道画面内缩百分比
  const safe = {
    x1: win.x + (sa.left / 100) * win.w,
    y1: win.y + (sa.top / 100) * win.h,
    x2: win.x + win.w - (sa.right / 100) * win.w,
    y2: win.y + win.h - (sa.bottom / 100) * win.h,
  };
  const violations = [];
  for (const ov of overlays) {
    if (!ov.required) continue; // 仅必要提示强制
    const b = ov.box; // {x,y,w,h} 母版归一化坐标
    const inside = b.x >= safe.x1 - 1e-9 && b.y >= safe.y1 - 1e-9 &&
                   b.x + b.w <= safe.x2 + 1e-9 && b.y + b.h <= safe.y2 + 1e-9;
    if (!inside) {
      const clippedHorizontally = b.x < win.x - 1e-9 || b.x + b.w > win.x + win.w + 1e-9;
      violations.push({
        overlay_id: ov.id, shot_id: ov.shot_id, text: ov.text,
        reason: clippedHorizontally ? 'cropped_by_aspect' : 'outside_safe_area',
        detail: clippedHorizontally
          ? `竖版裁切遮住了必要提示「${ov.text}」`
          : `必要提示「${ov.text}」超出渠道安全区`,
        box: b, safe,
      });
    }
  }
  return { ok: violations.length === 0, violations, safe, window: win };
}

// ---------- 发布依赖检查 ----------
/** 发布前重新检查：素材授权/来源 + 规则有效性 + 各渠道安全区 */
function publishCheck(store, project, asOfDate) {
  const problems = [];
  const clips = store.find('clips', (c) => c.project_id === project.id);
  for (const clip of clips) {
    const f = clipFreshness(store, clip);
    if (f.status === 'license_revoked') {
      problems.push({ type: 'license_revoked', clip_id: clip.id, material_id: clip.material_id,
        detail: `素材 ${clip.material_id} 授权已撤回（${clip.source_mode === 'copy' ? '复制件仍受母素材授权约束' : '引用'}）` });
    } else if (f.status === 'source_missing') {
      problems.push({ type: 'source_missing', clip_id: clip.id, material_id: clip.material_id,
        detail: `素材 ${clip.material_id} 来源已失效` });
    } else if (f.status === 'material_deleted') {
      problems.push({ type: 'material_deleted', clip_id: clip.id, material_id: clip.material_id });
    }
  }
  // 规则：项目引用的规则在发布日仍须有效
  const shots = store.find('shots', (s) => s.project_id === project.id);
  for (const shot of shots) {
    for (const ov of shot.overlays || []) {
      if (ov.rule_id) {
        const rule = store.get('rules', ov.rule_id);
        if (!rule) problems.push({ type: 'rule_deleted', shot_id: shot.id, rule_id: ov.rule_id });
        else if (!(rule.effective_from <= asOfDate && (rule.effective_to == null || rule.effective_to > asOfDate))) {
          problems.push({ type: 'rule_expired', shot_id: shot.id, rule_id: ov.rule_id,
            detail: `规则「${rule.title}」在 ${asOfDate} 已失效（可能已被修订替代）` });
        }
      }
    }
  }
  // 渠道安全区：每个绑定渠道必须有针对当前脚本版本的通过记录
  const bindings = store.find('project_channels', (pc) => pc.project_id === project.id);
  for (const b of bindings) {
    const check = store.findOne('safe_area_checks', (c) =>
      c.project_id === project.id && c.channel_id === b.channel_id && c.script_version === project.script_version);
    if (!check) {
      problems.push({ type: 'safe_area_unchecked', channel_id: b.channel_id,
        detail: '脚本已变更，需重新执行渠道安全区检查（不能复用旧缩略图结论）' });
    } else if (!check.ok) {
      problems.push({ type: 'safe_area_violation', channel_id: b.channel_id, violations: check.violations });
    }
  }
  return { ok: problems.length === 0, problems, checked_at: new Date().toISOString(), as_of: asOfDate };
}

// ---------- 导出快照（不可变） ----------
/**
 * 导出保留制作时的真实信息：房型描述、规则、素材版本、字幕。
 * 之后房型描述更新、规则修订都不回写旧导出。
 * 周边推荐必须有来源(source)，无依据不生成。
 */
function buildExportSnapshot(store, project, version, channel, by) {
  const roomTypes = store.find('room_types', (rt) => rt.property_id === project.property_id);
  const asOf = version.created_at.slice(0, 10);
  const shots = store.find('shots', (s) => s.project_id === project.id);
  const clips = store.find('clips', (c) => c.project_id === project.id);
  const subtitles = [];
  for (const shot of shots.sort((a, b) => a.idx - b.idx)) {
    for (const ov of shot.overlays || []) {
      if (ov.type === 'subtitle') subtitles.push({ shot_id: shot.id, idx: shot.idx, text: ov.text });
    }
  }
  // 周边推荐：仅采纳带来源的条目
  const nearby = (project.nearby_recommendations || []).filter((n) => n.source && n.source.url);
  const dropped = (project.nearby_recommendations || []).length - nearby.length;

  return {
    project_id: project.id,
    project_version_id: version.id,
    version_no: version.version_no,
    channel_id: channel ? channel.id : null,
    produced_at: new Date().toISOString(),
    produced_by: by,
    // —— 制作时快照，不随后续更新 ——
    room_type_descriptions: Object.fromEntries(roomTypes.map((rt) => [rt.id, { name: rt.name, description: rt.description, description_version: rt.description_version }])),
    rules: roomTypes.flatMap((rt) => effectiveOn(store.data.rules, rt.id, asOf))
      .map((r) => ({ id: r.id, room_type_id: r.room_type_id, title: r.title, text: r.text, revision: r.revision, effective_from: r.effective_from })),
    materials: clips.map((c) => {
      const m = store.get('materials', c.material_id);
      return { clip_id: c.id, material_id: c.material_id, source_mode: c.source_mode,
        material_version: c.source_mode === 'copy' ? c.copied_snapshot.version : c.material_version,
        content_hash: c.source_mode === 'copy' ? c.copied_snapshot.content_hash : (m ? m.content_hash : null),
        license_status_at_production: m ? m.license.status : 'unknown' };
    }),
    subtitles,
    shot_list: shots.sort((a, b) => a.idx - b.idx).map((s) => ({ id: s.id, idx: s.idx, name: s.name, duration: s.duration })),
    nearby_recommendations: nearby,
    nearby_dropped_for_no_source: dropped,
  };
}

module.exports = {
  AUTHORING_ASPECT,
  effectiveOn, reviseRule,
  validateSpaceLabel, SHARED_SPACE_TAGS,
  makeClip, clipFreshness,
  visibleWindow, checkSafeArea,
  publishCheck, buildExportSnapshot,
};
