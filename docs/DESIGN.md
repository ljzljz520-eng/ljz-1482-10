# 设计文档 —— 民宿房源视频编辑台

## 1. 领域模型

```
房源 Property 1───n 房型 RoomType
                      ├── 设施 Facility      (room_type_id + effective_from/to)
                      ├── 窗景 WindowView    (room_type_id + effective_from/to)
                      └── 规则 Rule          (room_type_id + effective_from/to + revision 链)
房源 1───n 素材 Material
             ├── scope=property_shared  公共素材（大厅/外立面/走廊…）
             └── scope=room_private     私有素材（绑定具体房型）
项目 Project 1───n 镜头 Shot ─── overlay(字幕/必要提示, 母版归一化坐标)
项目 1───n 片段 Clip ─── 素材（引用 reference / 复制 copy）
项目 n───n 渠道 Channel（横版16:9 / 竖版9:16 + 安全区）
项目 1───n 版本 ProjectVersion / 导出 Export（不可变快照）
```

### 公共素材与私有空间

一栋房源的多个房型**共用大厅素材**，因此：

- 素材分两级作用域：`property_shared`（房源级公共）与 `room_private`（房型级私有）。
- 公共素材**不允许**绑定单一房型，也**不允许**被标注为 `private` 空间；
  含 `lobby/facade/corridor/garden/parking` 等公共空间标签的素材同样拒绝标私有
  （`domain.validateSpaceLabel`，`POST /api/materials/:id/space-label` 返回 409）。
- 设施、窗景、规则一律挂在 `room_type_id` 上，并带 `effective_from/effective_to`，
  按日期解析（`domain.effectiveOn`），房型之间天然隔离。

### 入住规则修订

规则采用**修订链**而非原地覆盖：

- `POST /api/room-types/:id/rules` 带 `revises` 即生成 `revision+1` 的新规则；
- 旧规则的 `effective_to` 自动截断到新规则生效日，`superseded_by` 指向新版，历史可追溯；
- 任意日期 `?at=YYYY-MM-DD` 都能还原当日有效规则——字幕里引用了某条规则
  （`overlay.rule_id`）时，发布检查会验证该规则在发布日仍有效，否则拦截
  （`rule_expired`）。

## 2. 公共片段：引用 vs 复制

| 维度 | 引用 reference | 复制 copy |
|---|---|---|
| 存储 | 只存 `material_id` + pin 的 `material_version` | 素材内容快照进 `clip.copied_snapshot` |
| 母素材更新 | clip 读取时呈现 `stale`，**显式**「跟随母版」才升级，不静默替换 | 不受影响，保持复制时内容 |
| 授权撤回 | 发布检查拦截 | **同样拦截**——复制不转移授权，`origin_material_id` 追溯母素材授权状态 |
| 来源失效（原图被删） | 拦截 | 拦截（授权/来源语义在母素材上） |
| 历史复现 | 靠 pin 的 `material_version` + 导出快照中的 `content_hash` | 天然稳定，快照即在项目内 |
| 适用 | 大厅等公共片段、需要统一换版的素材 | 需要冻结画面的定制片段 |

**母素材更新**：`PATCH /api/materials/:id` 内容变化 → `version+1`。引用型 clip
因 pin 的版本落后而显示 `stale`（列表中"有新版"），编辑可逐个
`POST /api/clips/:id/follow-master` 显式升级——绝不在打开项目时静默换内容，
保证"我上次看到的"和"这次导出的"一致。

**授权撤回**：`license.status=revoked` 后，该素材不能再进时间线；已在时间线中的
引用**和复制**件都会在 `publishCheck` 中被拦截（422 + 明细）。复制不是授权的避风港。

**历史复现**：导出快照记录每个 clip 制作时实际使用的
`material_version` 与 `content_hash`、当时的授权状态。即便母素材后续更新或授权被
撤回，旧导出依然如实呈现"制作那一刻"的事实，可据此复现或举证。

## 3. 渠道级安全区（横竖版共用脚本）

脚本母版统一按 16:9 编写，overlay 用母版归一化坐标。竖版 9:16 由母版**居中裁切**：

```
可见窗口(9:16) = 中间 31.6% 宽 × 全高     （domain.visibleWindow）
安全区 = 可见窗口 内缩渠道配置的 上/右/下/左 %
必要提示(required overlay) 必须完整落在安全区内，否则违规：
  - cropped_by_aspect   被竖版裁切直接遮掉
  - outside_safe_area   在画面内但越出安全区（会被平台 UI 遮挡）
```

关键约束：**不能仅复用横版缩略图/旧结论认为竖版可用**。

- 检查按「项目 × 渠道 × 脚本版本」记录（`safe_area_checks`）；
- 任何镜头/字幕修改都会递增 `script_version`，旧检查结论自动失效；
- 发布时若某绑定渠道没有针对当前脚本版本的通过记录 → `safe_area_unchecked` 拦截；
- 检查由媒体工作器异步执行（`safe_area_check` job），结果含违规明细与坐标，前端高亮。

## 4. 并发：两人调整同一镜头

镜头级**乐观锁**：`Shot.version`，修改必须带 `base_version`。

- 版本一致 → 应用修改并 `version+1`；
- 不一致 → `409 {current: 最新镜头}`，调用方拿到最新数据合并后重试。
  （验收场景：阿宁与小周同改一个镜头，后到者 409，前端提示并刷新。）

## 5. 上传重试与恢复

**分块上传，两级幂等**：

- 会话级：`POST /api/uploads` 带 `idempotency_key`，网络重试重复创建 → 返回原会话；
- 块级：`PUT /api/uploads/:id/chunks/:n` 带 `x-chunk-key`，同块同 key 重试 →
  `deduplicated`；同块不同 key → `409 chunk_key_conflict`；
- `complete` 校验块完整性（缺块 409 并列出 `missing`），重复 complete 幂等。

**恢复上次项目**：会话表记录 `last_project_id`；`GET /api/session/last-project`
**实时重新校验 ACL**——上次会话后权限被收回 → 403；被降级为 viewer → 可恢复但只读。
权限判断从不缓存。

## 6. 首页游标与发布前重检

- 首页游标引用的是 **项目 + 版本**（`homepage_cursors.version_id`），
  首页展示固定在指定版本，不会因工作区继续编辑而漂移；
- `GET /api/homepage` 实时附带依赖健康度（素材授权/来源、规则有效性、安全区），
  仅提示不阻塞展示；
- `POST /api/projects/:id/publish` **强制执行** `publishCheck`：
  任一素材授权撤回/来源失效、引用规则在发布日失效、渠道未通过当前脚本版本的
  安全检查 → 422 全量问题清单，缺一不可放行。

## 7. 导出：制作时的真实信息，不可变

`Export` 在发布时生成，之后任何更新都不回写：

- **房型描述**：快照保存 `description_version` 与文本；之后房型描述翻新，
  旧导出仍显示制作时描述（不自动套用当前描述）；
- **规则**：快照保存发布日有效规则及 revision；
- **素材**：快照保存制作时版本、内容哈希、授权状态；
- **周边推荐**：只采纳带 `source.url` 的条目，无依据的推荐直接丢弃并计数
  （`nearby_dropped_for_no_source`）——不生成没有来源的"附近推荐"。

## 8. 媒体工作器

进程内持久化队列（任务落库，重启可续），四类产物：

| 任务 | 产物 |
|---|---|
| `render_subtitles` | SRT 字幕（按镜头时序累计时间轴） |
| `shot_list`        | 镜头清单（含素材引用方式与版本） |
| `preview`          | 预览清单（模拟转码产物地址） |
| `safe_area_check`  | 渠道安全区检查记录 |

## 9. 持久化与可替换性

`server/store.js` 为集合式 JSON 文档库，写操作 `tmp+rename` 原子落盘
（`data/homestay.json`）。所有路由只依赖 store 的集合 API，
可平移到 SQLite/Postgres 而不动业务层。
