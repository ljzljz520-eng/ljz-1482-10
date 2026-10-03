'use strict';
/**
 * 持久化存储层：JSON 文档库 + 原子写（tmp + rename）。
 * 集合式 API，便于日后替换为 SQLite/Postgres。
 */
const fs = require('fs');
const path = require('path');

const COLLECTIONS = [
  'users', 'properties', 'room_types', 'materials',
  'facilities', 'window_views', 'rules',
  'projects', 'project_versions', 'shots', 'clips',
  'channels', 'project_channels', 'safe_area_checks',
  'exports', 'upload_sessions', 'acls', 'sessions',
  'homepage_cursors', 'jobs',
];

function createStore(file) {
  let data;
  if (file && fs.existsSync(file)) {
    data = JSON.parse(fs.readFileSync(file, 'utf8'));
    for (const c of COLLECTIONS) if (!data[c]) data[c] = [];
    if (!data.counters) data.counters = {};
  } else {
    data = { counters: {} };
    for (const c of COLLECTIONS) data[c] = [];
    if (file) fs.mkdirSync(path.dirname(file), { recursive: true });
  }

  function persist() {
    if (!file) return;
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
    fs.renameSync(tmp, file); // 同目录 rename，原子替换
  }

  function nextId(prefix) {
    data.counters[prefix] = (data.counters[prefix] || 0) + 1;
    return `${prefix}_${String(data.counters[prefix]).padStart(4, '0')}`;
  }

  return {
    data,
    persist,
    nextId,
    insert(coll, obj) {
      data[coll].push(obj);
      persist();
      return obj;
    },
    get(coll, id) {
      return data[coll].find((r) => r.id === id) || null;
    },
    find(coll, pred) {
      return data[coll].filter(pred);
    },
    findOne(coll, pred) {
      return data[coll].find(pred) || null;
    },
    update(coll, id, patch) {
      const row = this.get(coll, id);
      if (!row) return null;
      Object.assign(row, typeof patch === 'function' ? patch(row) : patch);
      persist();
      return row;
    },
    remove(coll, id) {
      const i = data[coll].findIndex((r) => r.id === id);
      if (i >= 0) data[coll].splice(i, 1);
      persist();
    },
  };
}

module.exports = { createStore };
