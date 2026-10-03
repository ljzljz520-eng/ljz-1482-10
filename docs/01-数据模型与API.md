# 数据模型与 API

## 实体关系

```
properties 1──* room_types
   │              ├─* room_rule_versions（只增不改的规则修订，含生效窗口）
   │              └─* room_features（facility / window_view，含 valid_from/valid_to）
   ├─* property_members(user, owner/editor/viewer)
   ├─* assets 1──* asset_versions 1──1 asset_licenses（可撤回）
   └─* projects 1──* project_versions（每次保存追加版本）
                     └─ timeline_json（镜头文档）
home_cursors(user, project) -> project_versions(id)   # 首页游标钉住确切版本
jobs *──1 project_versions ; exports 1──* export_outputs
upload_sessions（可断点续传）
```

## 空间作用域（shared / private）

* `assets.scope='shared'` 的素材 `room_type_id` 必须为 NULL（大厅、茶室、院子）。
  登记接口对 shared+room 直接返回 `SHARED_HAS_ROOM`。
* `scope='private'` 必须绑定本房源下的具体房型；时间线镜头重复携带 scope/room，
  保存时二次校验，错误码 `SHARED_ASSET_MARKED_PRIVATE` / `ROOM_MISMATCH`。
* 设施、窗景不是"房源级标签"：`room_features` 必带 `room_type_id` 与有效日期窗口。

## 规则修订

* `POST /api/room-types/{id}/rules` 总是新增一行，`version` 单调递增；
  上一版开放窗口自动在新生效日前一天关闭，保证任一发布日**至多一条生效版本**。
* 时间线规则卡片保存的是 `rule_version_id`（不可变指针），发布时解析发布日的生效版本：
  * 卡片版本发布日不生效 → `RULE_NOT_EFFECTIVE`；
  * 发布日已有更新的生效版本 → `RULE_REVISION_STALE`（必须人工确认重挂，不自动跟版）。

## 时间线文档（project_versions.timeline_json）

```jsonc
{
  "channels": ["landscape_169", "douyin"],
  "items": [{
    "clip_id": "c1",
    "asset_id": 2, "asset_version": 1,   // 钉住母素材版本
    "mode": "reference | copy",          // 引用 / 复制到项目
    "scope": "private", "room_type_id": 1,
    "duration": 4, "caption": "字幕文案",
    "embedded_prompts": [/* 烧录在素材画面里的必要提示，16:9 画布坐标 */],
    "overlays": [
      {"type": "caption"},               // 字幕：按渠道字幕带自动排版
      {"type": "rule_card", "room_type_id": 1, "rule_version_id": 2,
       "channels": {"douyin": {"x":0.1,"y":0.1,"w":0.8,"h":0.18}}},
      {"type": "facility_card|window_card", "feature_id": 3, "room_type_id": 1,
       "channels": {"douyin": {"x":0.1,"y":0.31,"w":0.8,"h":0.14}}},
      {"type": "recommendation", "source_ref": "https://..."} // 必须有依据
    ]
  }]
}
```

## 主要接口

| 方法与路径 | 说明 |
|---|---|
| `GET /api/home` | 项目列表，带**游标版本号**（不是最新版本） |
| `POST /api/projects/{id}/resume` | 恢复上次项目：当场重查成员资格（变更→403） |
| `POST /api/projects/{id}/timeline` | 乐观锁保存，body 带 `base_version`；冲突→409+current_version |
| `POST /api/projects/{id}/copy-clip` | 镜头由引用转复制 |
| `GET /api/projects/{id}/checks?as_of=` | 发布前依赖 + 安全区报告（error 阻断 / warning 提示） |
| `POST /api/projects/{id}/publish` | 发布门通过后入队；默认内联跑工作器，`WORKBENCH_INLINE=0` 只入队 |
| `POST /api/uploads` · `PUT .../chunks/{i}` · `POST .../complete` | 分片上传，重试幂等 |
| `POST /api/assets/{id}/withdraw` · `.../source-status` | 授权撤回 / 来源失效登记 |
| `GET /api/exports/{id}` | 不可变导出：快照、检查结果、产出清单 |

## 状态码约定

403（角色不足或成员资格变化，如 `MEMBERSHIP_REQUIRED`）、409（`VERSION_CONFLICT`）、
422（时间线/发布门校验失败，body 附 `issues`）。
