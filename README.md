# 民宿房源视频编辑台

从空仓库搭建的、面向**单栋多房型民宿**的房源视频编辑台。Web 端维护房型、入住规则、
设施/窗景与素材时间线；后台 API 持久化项目；媒体工作器输出**字幕（SRT）、镜头清单（CSV）、
接触印片/故事板（SVG，可选 ffmpeg 输出 MP4）**。

## 快速开始

```bash
python3 -m scripts.seed        # 初始化演示数据（可重复执行，会清空 data/ 重建）
python3 -m backend.server      # http://127.0.0.1:8000
# 或：./run_dev.sh

# 独立工作器（与 API 分进程时）：
WORKBENCH_INLINE=0 python3 -m backend.server   # 只入队
python3 -m worker                              # 轮询并渲染
```

零第三方依赖（Python 3.11 标准库）；`ffmpeg` 存在时自动增强预览。

## 演示账号（种子数据）

| 用户 | 身份 | 用途 |
|---|---|---|
| alice | 房东（owner） | 修订规则、管理成员 |
| bob | 剪辑（editor） | 编辑时间线、发布 |
| evelyn | 剪辑（editor） | 与 bob 模拟两人改同一镜头 → 409 |
| carol | 非成员 | 验证"恢复上次项目时权限改变"→ 403 |

## 目录

```
backend/   db(表结构) · store(版本化仓储) · domain(安全区/作用域/依赖检查)
           · storage(内容寻址 blob) · api(WSGI) · server(静态+API)
worker/    render：字幕/镜头清单/接触印片/故事板(+ffmpeg MP4)，渲染前复检
web/       原生 JS 单页：首页游标、房型规则、素材库、上传、时间线、导出查看
scripts/   seed.py 演示数据
tests/     17 个验收用例（5 大场景 + 核心不变量）
docs/      中文设计与验收说明
```

## 跑测试

```bash
python3 -m unittest discover -s tests -v
```

## 关键业务约束（详见 docs/）

1. **公共/私有空间**：大厅等公共素材永远不能被标成某房型私有空间（写入与发布双重拦截，
   错误码 `SHARED_ASSET_MARKED_PRIVATE`）。设施、窗景、规则卡片都绑定**具体房型 + 有效期**。
2. **引用 vs 复制**：引用钉住母素材版本（母素材更新只告警不替换；授权撤回则发布阻断）；
   复制进项目的镜头独立持有、不因母素材更新而移动。历史导出靠快照 + 版本行 + 内容寻址字节
   精确复现，旧导出**绝不**套用当前房型描述，也不出现无依据的周边推荐。
3. **横竖版共用脚本**：一份 16:9 脚本，竖版按居中裁切推导。内嵌必要提示先过裁切映射、
   再过渠道安全区；卡片必须提供**每渠道独立坐标**；字幕按渠道字幕带自动重排。
   "缩略图能看"不构成可用，`thumbnail_only` 直接阻断发布。
4. **发布门 + 工作器复检**：点发布先做依赖/规则/安全区全量检查；工作器取到任务时**再次**
   复检——排队期间授权撤回或来源失效，任务照样失败。
5. **首页游标引用确切项目版本**，恢复上次项目时重新做成员权限校验（权限变更 → 403，
   而非静默降级）。

## 错误码速查

| 错误码 | 含义 |
|---|---||
| `SHARED_ASSET_MARKED_PRIVATE` | 大厅等公共素材被标成房型私有空间 |
| `ROOM_MISMATCH` / `PRIVATE_CLIP_NEEDS_ROOM` | 私有镜头房型绑定错误/缺失 |
| `CROP_CLIPS_ESSENTIAL` | 竖版裁切会遮掉素材内必要提示 |
| `OVERLAY_NO_CHANNEL_GEOMETRY` / `OVERLAY_OUTSIDE_SAFE` | 卡片缺渠道坐标 / 越出安全区 |
| `THUMBNAIL_USED_AS_MEDIA` | 只有缩略图代理，无完整素材 |
| `LICENSE_WITHDRAWN/EXPIRED/MISSING` | 授权撤回/过期/缺失 |
| `PHOTO_SOURCE_UNREACHABLE` | 照片来源失效 |
| `RULE_REVISION_STALE` / `RULE_NOT_EFFECTIVE` | 规则卡片落后于修订 / 发布日不生效 |
| `FEATURE_INVALID` | 设施/窗景在发布日不在有效期 |
| `RECOMMENDATION_UNSUPPORTED` | 周边推荐无依据来源 |
| `VERSION_CONFLICT`(409) | 两人改同一镜头，后保存者需刷新合并 |
| `MEMBERSHIP_REQUIRED`(403) | 权限已变更，无法恢复项目 |
