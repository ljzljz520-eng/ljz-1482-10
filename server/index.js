'use strict';
const path = require('path');
const { createStore } = require('./store');
const { createWorker } = require('./worker');
const { createApp } = require('./app');

const PORT = process.env.PORT || 3000;
const DB_FILE = process.env.DB_FILE || path.join(__dirname, '..', 'data', 'homestay.json');

const store = createStore(DB_FILE);
const worker = createWorker(store);
const app = createApp(store, worker);

app.use(require('express').static(path.join(__dirname, '..', 'public')));

app.listen(PORT, () => {
  console.log(`民宿房源视频编辑台 API 已启动: http://localhost:${PORT}`);
  console.log(`数据文件: ${DB_FILE}`);
});
