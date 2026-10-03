# 民宿房源视频编辑台

Web 端维护房型、入住规则和素材时间线；后台 API 持久化项目；媒体工作器输出字幕、
镜头清单及预览。

## 快速开始

```bash
npm install     # 仅依赖 express
npm start       # http://localhost:3000 （数据落盘 data/homestay.json）
npm test        # 13 个验收场景测试
```

首次打开页面会自动创建三个演示用户（owner/editor/viewer），右上角可切换身份，
用于演示权限变化与协作冲突。

## 结构

```
server/
  index.js    入口（API + 静态前端 + 媒体工作器）
  app.js      全部 REST 路由
  domain.js   领域逻辑：规则有效期/素材作用域/引用vs复制/安全区/发布检查/导出快照
  store.js    JSON 文档库（原子写，可替换 SQLite）
  worker.js   媒体工作器：字幕 SRT、镜头清单、预览、安全区检查
  auth.js     用户识别 + 项目级 ACL（实时校验，不缓存）
public/       单页前端（房源/素材/时间线/渠道/发布 五个页签）
test/         验收测试（node:test，内存库 + 真实 HTTP）
docs/DESIGN.md 设计取舍详述
```

## 验收场景对照

| 场景 | 实现 | 测试 |
|---|---|---|
| 入住规则修订 | 修订链 + 有效期截断，按日期还原 | `入住规则修订…` |
| 照片来源失效 | `invalidate-source` → 发布 422；旧导出仍记录制作时 `active` | `照片来源失效…` |
| 两人调整同一镜头 | 镜头乐观锁 `base_version`，冲突 409 返回最新 | `两人同时调整…` |
| 上传重试 | 会话级 `idempotency_key` + 块级 `x-chunk-key` 幂等 | `分块上传…` |
| 恢复上次项目权限改变 | 恢复时实时重查 ACL，收回即 403 | `恢复上次项目…` |
| 大厅素材不可标私有 | 公共作用域/公共标签双重拦截 409 | `大厅公共素材…` |
| 竖版裁切必要提示 | 渠道级安全区检查（裁切窗口 ∩ 安全区） | `竖版裁切…` |
| 不可复用缩略图结论 | 检查绑定脚本版本，脚本变更后必须重检 | `脚本变更后…` |
| 首页游标引用版本 | 游标指向 项目+版本，读取时附依赖健康度 | `首页游标…` |
| 发布前重检依赖 | `publishCheck` 强制拦截授权/来源/规则/安全区 | 多个测试 |
| 旧导出保留真实信息 | 导出不可变快照，不套用当前房型描述 | `导出快照…` |
| 无依据周边不推荐 | 无 `source.url` 的推荐丢弃并计数 | 同上 |

## API 一览（节选）

```
POST /api/properties/:id/room-types            房型
POST /api/room-types/:id/rules                 规则（revises=修订）
GET  /api/room-types/:id/rules?at=YYYY-MM-DD   当日有效规则
POST /api/materials/:id/space-label            空间标注（公共→私有 会被拒）
POST /api/materials/:id/revoke-license         授权撤回
POST /api/projects/:id/clips                   加片段 source_mode=reference|copy
POST /api/clips/:id/follow-master              引用片段显式跟随母版
PATCH /api/shots/:id                           乐观锁（base_version）
POST /api/projects/:id/channels/:cid/check     渠道安全区检查（异步 job）
POST /api/projects/:id/publish                 发布（强制依赖重检 → 不可变导出）
PUT  /api/homepage/cursor                      首页游标（项目+版本）
GET  /api/session/last-project                 恢复上次项目（实时权限校验）
POST /api/uploads  PUT /api/uploads/:id/chunks/:n  幂等分块上传
```

详细设计取舍（引用 vs 复制、安全区算法、导出快照语义）见 [docs/DESIGN.md](docs/DESIGN.md)。
