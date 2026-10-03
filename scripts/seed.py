"""Seed a demo property with shared + private media, rule revisions and a
draft project timeline.  Re-runnable: wipes data/ first."""
from __future__ import annotations

import json
import os
import shutil

from backend import store
from backend.db import init_db
from backend.storage import ensure_dirs, put_bytes


DEMO_DATE = "2026-10-03"


def reset() -> None:
    from backend.db import DB_PATH
    for p in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    root = os.path.join(os.path.dirname(DB_PATH), "..", "storage")
    for sub in ("chunks",):
        d = os.path.join(root, sub)
        if os.path.isdir(d):
            shutil.rmtree(d)
    ensure_dirs()
    init_db()


def main() -> None:
    reset()

    alice = store.create_user("alice", "房东 林栖")
    bob = store.create_user("bob", "剪辑 小舟")
    carol = store.create_user("carol", "外包剪辑（仅本房源）")

    pid = store.create_property("栖迟民宿·西湖一店", "杭州市西湖区青芝坞 12 号")
    store.add_member(pid, alice, "owner")
    store.add_member(pid, bob, "editor")
    # carol intentionally gets NO membership: used to verify resume 403

    king = store.create_room_type(pid, "山景大床房")
    twin = store.create_room_type(pid, "榻榻米双床房")

    # rule revision v1 -> v2 (check-in time amended, v1 closed)
    r1 = store.add_rule_revision(king,
        {"check_in": "14:00 后", "check_out": "次日 12:00 前",
         "pets": "不可携带宠物", "smoking": "全面禁烟",
         "note": "v1 初版规则"}, "2026-01-01", None, alice)
    r2 = store.add_rule_revision(king,
        {"check_in": "15:00 后", "check_out": "次日 11:00 前",
         "pets": "小型犬可（需提前申请）", "smoking": "全面禁烟",
         "note": "v2 旺季调整入住/退房时间"}, "2026-10-01", None, alice)

    store.add_feature(king, "facility", "65寸投影", "2026-01-01", None, "现场核验")
    store.add_feature(king, "window_view", "直面北高峰山景", "2026-01-01", None, "现场核验")
    store.add_feature(king, "facility", "露台泡池（季节性，11月停用）",
                      "2026-04-01", "2026-10-31", "运营台账")
    store.add_feature(twin, "window_view", "院内天井景观", "2026-01-01", None, "现场核验")

    lobby_sha, lobby_sz = put_bytes(b"FAKE-MP4-LOBBY-SHARED-MEDIA-BYTES")
    king_sha, king_sz = put_bytes(b"FAKE-MP4-KING-ROOM-PRIVATE-MEDIA")
    twin_sha, twin_sz = put_bytes(b"FAKE-MP4-TWIN-ROOM-PRIVATE-MEDIA")
    thumb_sha, thumb_sz = put_bytes(b"THUMBNAIL-PROXY-BYTES-ONLY")

    lobby = store.create_asset(property_id=pid, kind="video",
        title="大厅与共享茶室（公共）", scope="shared", room_type_id=None,
        space_label="大厅", sha256=lobby_sha, size_bytes=lobby_sz,
        source_uri="media://shot/2026-09/lobby_master.mov",
        width=1920, height=1080, duration=8, thumbnail_only=0, created_by=bob)
    king_room = store.create_asset(property_id=pid, kind="video",
        title="山景大床房室内", scope="private", room_type_id=king,
        space_label="山景大床房", sha256=king_sha, size_bytes=king_sz,
        source_uri="media://shot/2026-09/king_master.mov",
        width=1920, height=1080, duration=6, thumbnail_only=0, created_by=bob)
    twin_room = store.create_asset(property_id=pid, kind="video",
        title="榻榻米双床房室内", scope="private", room_type_id=twin,
        space_label="榻榻米双床房", sha256=twin_sha, size_bytes=twin_sz,
        source_uri="https://old-photo-host.example/twin.mp4",
        width=1920, height=1080, duration=6, thumbnail_only=0, created_by=bob)
    thumb = store.create_asset(property_id=pid, kind="image",
        title="早餐区（仅有缩略图代理）", scope="shared", room_type_id=None,
        space_label="餐厅", sha256=thumb_sha, size_bytes=thumb_sz,
        source_uri="media://proxy/breakfast_thumb.jpg",
        width=320, height=180, duration=None, thumbnail_only=1, created_by=bob)

    for aid in (lobby, king_room, twin_room):
        av = store.get_asset_version(aid, 1)
        store.add_license(av["id"], holder="栖迟民宿（自持素材）",
                          valid_from="2026-01-01", valid_to=None,
                          note="房东自持，授权员工剪辑")
    av = store.get_asset_version(thumb, 1)
    store.add_license(av["id"], "代理图", "2026-01-01", None, "缩略图不可作为成片")

    # a complete, publishable timeline (caption band sits in each channel's
    # caption-safe area; embedded prompts centered for vertical crops)
    timeline = {
        "channels": ["landscape_169", "douyin"],
        "items": [
            {"clip_id": "c1", "asset_id": lobby, "asset_version": 1,
             "mode": "reference", "scope": "shared", "room_type_id": None,
             "duration": 4, "caption": "栖迟民宿·西湖一店",
             "embedded_prompts": [
                 # centered so it survives the 9:16 side-crop AND stays inside
                 # douyin's 0.075 / 0.20 safe insets after mapping
                 {"label": "消防疏散示意", "x": 0.375, "y": 0.21, "w": 0.25, "h": 0.07,
                  "essential": True}],
             "overlays": [
                 {"type": "caption", "label": "片头名", "essential": True}]},
            {"clip_id": "c2", "asset_id": king_room, "asset_version": 1,
             "mode": "reference", "scope": "private", "room_type_id": king,
             "duration": 4, "caption": "山景大床房｜15:00 入住 / 次日 11:00 退房",
             "embedded_prompts": [],
             "overlays": [
                 {"type": "rule_card", "label": "入住规则（2026-10 版）",
                  "room_type_id": king, "rule_version_id": r2, "essential": True,
                  "channels": {
                      "landscape_169": {"x": 0.06, "y": 0.08, "w": 0.40, "h": 0.26},
                      "douyin": {"x": 0.10, "y": 0.10, "w": 0.80, "h": 0.18}}},
                 {"type": "window_card", "label": "北高峰山景",
                  "room_type_id": king, "feature_id": 2, "essential": False,
                  "channels": {
                      "landscape_169": {"x": 0.58, "y": 0.08, "w": 0.36, "h": 0.22},
                      "douyin": {"x": 0.10, "y": 0.31, "w": 0.80, "h": 0.14}}},
                 {"type": "caption", "label": "字幕", "essential": True}]},
            {"clip_id": "c3", "asset_id": twin_room, "asset_version": 1,
             "mode": "reference", "scope": "private", "room_type_id": twin,
             "duration": 4, "caption": "榻榻米双床房，适合好友同行",
             "embedded_prompts": [],
             "overlays": [
                 {"type": "caption", "label": "字幕", "essential": True}]},
        ],
    }
    proj = store.create_project(pid, "秋季房型推介 v1", bob, timeline)
    print(json.dumps({
        "property_id": pid, "users": {"alice": alice, "bob": bob, "carol": carol},
        "room_types": {"king": king, "twin": twin},
        "assets": {"lobby": lobby, "king": king_room, "twin": twin_room, "thumb": thumb},
        "rule_versions": {"v1": r1, "v2": r2},
        "project_id": proj,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
