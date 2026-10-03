'use strict';
/**
 * 媒体工作器：进程内持久化队列，输出字幕(SRT)、镜头清单、预览清单、安全区检查。
 * 任务落库，进程重启后可续跑；通过事件通知 API 层。
 */
const { EventEmitter } = require('events');
const domain = require('./domain');

function createWorker(store, { tickMs = 50 } = {}) {
  const emitter = new EventEmitter();
  let timer = null;

  const handlers = {
    /** 字幕：从各镜头 overlay(type=subtitle) 按时序生成 SRT */
    render_subtitles(job) {
      const shots = store.find('shots', (s) => s.project_id === job.payload.project_id)
        .sort((a, b) => a.idx - b.idx);
      let t = 0, n = 0;
      const lines = [];
      for (const shot of shots) {
        for (const ov of shot.overlays || []) {
          if (ov.type !== 'subtitle') continue;
          n += 1;
          const start = t, end = t + shot.duration;
          lines.push(`${n}\n${fmt(start)} --> ${fmt(end)}\n${ov.text}\n`);
        }
        t += shot.duration;
      }
      return { format: 'srt', cue_count: n, content: lines.join('\n') };
    },

    /** 镜头清单 */
    shot_list(job) {
      const shots = store.find('shots', (s) => s.project_id === job.payload.project_id)
        .sort((a, b) => a.idx - b.idx);
      const clips = store.find('clips', (c) => c.project_id === job.payload.project_id);
      return {
        count: shots.length,
        items: shots.map((s) => ({
          idx: s.idx, id: s.id, name: s.name, duration: s.duration,
          materials: clips.filter((c) => c.shot_id === s.id)
            .map((c) => ({ clip_id: c.id, material_id: c.material_id, mode: c.source_mode, version: c.material_version })),
          overlays: (s.overlays || []).map((o) => ({ id: o.id, type: o.type, required: !!o.required })),
        })),
      };
    },

    /** 预览：输出预览清单（模拟转码产物） */
    preview(job) {
      const shots = store.find('shots', (s) => s.project_id === job.payload.project_id);
      return {
        manifest: `preview-${job.payload.project_id}`,
        frames: shots.length * 2,
        url: `/previews/${job.payload.project_id}/${job.id}.m3u8`,
      };
    },

    /** 渠道级安全区检查：按渠道裁切与安全区重新计算，写入检查结果 */
    safe_area_check(job) {
      const { project_id, channel_id } = job.payload;
      const project = store.get('projects', project_id);
      const channel = store.get('channels', channel_id);
      if (!project || !channel) throw new Error('project/channel 不存在');
      const shots = store.find('shots', (s) => s.project_id === project_id);
      const overlays = shots.flatMap((s) => (s.overlays || []).map((o) => ({ ...o, shot_id: s.id })));
      const result = domain.checkSafeArea(overlays, channel);
      const record = {
        id: store.nextId('check'),
        project_id, channel_id,
        script_version: project.script_version,
        ok: result.ok, violations: result.violations,
        safe: result.safe, window: result.window,
        created_at: new Date().toISOString(),
      };
      store.insert('safe_area_checks', record);
      return record;
    },
  };

  function fmt(sec) {
    const h = String(Math.floor(sec / 3600)).padStart(2, '0');
    const m = String(Math.floor((sec % 3600) / 60)).padStart(2, '0');
    const s = String(Math.floor(sec % 60)).padStart(2, '0');
    const ms = String(Math.round((sec % 1) * 1000)).padStart(3, '0');
    return `${h}:${m}:${s},${ms}`;
  }

  function submit(type, payload) {
    const job = {
      id: store.nextId('job'), type, payload,
      status: 'queued', result: null, error: null,
      created_at: new Date().toISOString(), finished_at: null,
    };
    store.insert('jobs', job);
    schedule();
    return job;
  }

  function processNext() {
    const job = store.findOne('jobs', (j) => j.status === 'queued');
    if (!job) return;
    store.update('jobs', job.id, { status: 'running' });
    try {
      const result = handlers[job.type](job);
      store.update('jobs', job.id, { status: 'done', result, finished_at: new Date().toISOString() });
      emitter.emit('done', store.get('jobs', job.id));
    } catch (err) {
      store.update('jobs', job.id, { status: 'failed', error: err.message, finished_at: new Date().toISOString() });
      emitter.emit('failed', store.get('jobs', job.id));
    }
    if (store.findOne('jobs', (j) => j.status === 'queued')) schedule();
  }

  function schedule() {
    if (timer) return;
    timer = setTimeout(() => { timer = null; processNext(); }, tickMs);
    if (timer.unref) timer.unref();
  }

  return { submit, events: emitter, _processNext: processNext };
}

module.exports = { createWorker };
