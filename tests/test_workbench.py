"""Acceptance tests for the homestay video editing workbench.

Scenarios (from the product brief)
----------------------------------
1. 入住规则修订 -> stale rule card blocks publish until re-bound.
2. 照片来源失效 -> publish blocked; copy mode keeps history reproducible.
3. 两人调整同一镜头 -> optimistic 409, no lost update.
4. 上传重试 -> chunked upload idempotent, resume works.
5. 恢复上次项目时权限改变 -> 403 MEMBERSHIP_REQUIRED.
Plus core invariants: shared/private scope, channel safe-area gating,
thumbnail-only rejection, master update vs reference/copy, old exports
keep frozen truth, homepage cursor pins a version.
"""
import io
import json
import os
import tempfile
import unittest
import uuid

# isolate EVERYTHING before importing app modules
_tmp = tempfile.mkdtemp(prefix="wb-test-")
os.environ["WORKBENCH_DB"] = os.path.join(_tmp, "test.db")
os.environ["WORKBENCH_STORAGE"] = os.path.join(_tmp, "storage")

from backend import domain, store          # noqa: E402
from backend.api import app               # noqa: E402
from backend.db import init_db            # noqa: E402
from backend.storage import ensure_dirs, put_bytes  # noqa: E402


def call(method, path, user=None, body=None, raw=None, ctype="application/json"):
    payload = None
    if raw is not None:
        payload = raw
    elif body is not None:
        payload = json.dumps(body).encode()
    qs = path.split("?", 1)[1] if "?" in path else ""
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path.split("?", 1)[0],
        "QUERY_STRING": qs, "CONTENT_LENGTH": str(len(payload or b"")),
        "CONTENT_TYPE": ctype,
        "HTTP_X_USER_ID": str(user) if user else "",
        "wsgi.input": io.BytesIO(payload or b""),
    }
    captured = {}

    def start(status, headers):
        captured["status"] = int(status.split()[0])

    out = b"".join(app(environ, start))
    return captured["status"], json.loads(out.decode() or "null")


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        ensure_dirs()
        cls.alice = store.create_user("alice", "房东")
        cls.bob = store.create_user("bob", "剪辑甲")
        cls.evelyn = store.create_user("evelyn", "剪辑乙")
        cls.outsider = store.create_user("outsider", "离职外包")

        cls.pid = store.create_property("测试民宿", "测试地址")
        store.add_member(cls.pid, cls.alice, "owner")
        store.add_member(cls.pid, cls.bob, "editor")
        store.add_member(cls.pid, cls.evelyn, "editor")
        cls.king = store.create_room_type(cls.pid, "大床房")
        cls.twin = store.create_room_type(cls.pid, "双床房")

        cls.r1 = store.add_rule_revision(
            cls.king, {"check_in": "14:00", "check_out": "12:00",
                       "pets": "no", "smoking": "no"},
            "2026-01-01", None, cls.alice)
        cls.r2 = store.add_rule_revision(
            cls.king, {"check_in": "15:00", "check_out": "11:00",
                       "pets": "small_dog", "smoking": "no"},
            "2026-10-01", None, cls.alice)
        cls.f_view = store.add_feature(cls.king, "window_view", "山景",
                                       "2026-01-01", None, "")
        cls.f_pool = store.add_feature(cls.king, "facility", "泡池",
                                       "2026-04-01", "2026-08-31", "")

        lobby_sha, lobby_sz = put_bytes(b"LOBBY-SHARED-MEDIA")
        room_sha, room_sz = put_bytes(b"KING-PRIVATE-MEDIA")
        twinm_sha, twinm_sz = put_bytes(b"TWIN-PRIVATE-MEDIA")
        thumb_sha, thumb_sz = put_bytes(b"THUMB-ONLY")
        cls.lobby = store.create_asset(
            property_id=cls.pid, kind="video", title="大厅", scope="shared",
            room_type_id=None, space_label="大厅", sha256=lobby_sha,
            size_bytes=lobby_sz, source_uri="media://lobby", width=1920,
            height=1080, duration=4, thumbnail_only=0, created_by=cls.bob)
        cls.room = store.create_asset(
            property_id=cls.pid, kind="video", title="大床房内",
            scope="private", room_type_id=cls.king, space_label="大床房",
            sha256=room_sha, size_bytes=room_sz, source_uri="media://king",
            width=1920, height=1080, duration=4, thumbnail_only=0,
            created_by=cls.bob)
        cls.twin_media = store.create_asset(
            property_id=cls.pid, kind="video", title="双床房内",
            scope="private", room_type_id=cls.twin, space_label="双床房",
            sha256=twinm_sha, size_bytes=twinm_sz,
            source_uri="https://photos.example/twin.mp4", width=1920,
            height=1080, duration=4, thumbnail_only=0, created_by=cls.bob)
        cls.thumb = store.create_asset(
            property_id=cls.pid, kind="image", title="缩略图代理",
            scope="shared", room_type_id=None, space_label="餐厅",
            sha256=thumb_sha, size_bytes=thumb_sz, source_uri="media://thumb",
            width=320, height=180, duration=None, thumbnail_only=1,
            created_by=cls.bob)
        for aid in (cls.lobby, cls.room, cls.twin_media):
            av = store.get_asset_version(aid, 1)
            store.add_license(av["id"], "自持", "2026-01-01", None, "")
        av = store.get_asset_version(cls.thumb, 1)
        store.add_license(av["id"], "代理", "2026-01-01", None, "")

    def make_project(self, timeline=None, user=None):
        if timeline is None:
            timeline = self.good_timeline()
        st, d = call("POST", "/api/projects", user or self.bob,
                     {"property_id": self.pid, "title": "P", "timeline": timeline})
        self.assertEqual(st, 200, d)
        return d["project_id"]

    def good_timeline(self, channels=("landscape_169", "douyin")):
        def geos():
            return {ch: {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.18}
                    for ch in channels}
        return {"channels": list(channels), "items": [
            {"clip_id": "lobby", "asset_id": self.lobby, "asset_version": 1,
             "mode": "reference", "scope": "shared", "room_type_id": None,
             "duration": 3, "caption": "片头",
             "embedded_prompts": [{"label": "疏散图", "x": 0.375, "y": 0.21,
                                   "w": 0.25, "h": 0.07, "essential": True}],
             "overlays": [{"type": "caption", "label": "字幕"}]},
            {"clip_id": "king", "asset_id": self.room, "asset_version": 1,
             "mode": "reference", "scope": "private", "room_type_id": self.king,
             "duration": 3, "caption": "大床房",
             "embedded_prompts": [],
             "overlays": [
                 {"type": "caption", "label": "字幕"},
                 {"type": "rule_card", "label": "规则",
                  "room_type_id": self.king, "rule_version_id": self.r2,
                  "essential": True, "channels": geos()},
                 {"type": "window_card", "label": "山景",
                  "room_type_id": self.king, "feature_id": self.f_view,
                  "essential": False, "channels": geos()}]},
        ]}


    def _fresh_rule_timeline(self, channels=("landscape_169", "douyin")):
        """A private room asset + its own rule revision, independent of the
        shared class fixture so tests don't leak state into each other."""
        sha, sz = put_bytes(("ROOM-%s" % uuid.uuid4().hex).encode())
        aid = store.create_asset(
            property_id=self.pid, kind="video", title="独立房素材",
            scope="private", room_type_id=self.twin, space_label="双床房",
            sha256=sha, size_bytes=sz, source_uri="media://ind",
            width=1920, height=1080, duration=3, thumbnail_only=0,
            created_by=self.bob)
        av = store.get_asset_version(aid, 1)
        store.add_license(av["id"], "自持", "2026-01-01", None, "")
        rv = store.add_rule_revision(
            self.twin, {"check_in": "14:00", "check_out": "12:00",
                        "pets": "no", "smoking": "no"},
            "2026-01-01", None, self.alice)
        fv = store.add_feature(self.twin, "window_view", "天井",
                               "2026-01-01", None, "")
        def geos():
            return {ch: {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.18}
                    for ch in channels}
        tl = {"channels": list(channels), "items": [
            {"clip_id": "room", "asset_id": aid, "asset_version": 1,
             "mode": "reference", "scope": "private", "room_type_id": self.twin,
             "duration": 3, "caption": "独立房", "embedded_prompts": [],
             "overlays": [
                 {"type": "caption", "label": "字幕"},
                 {"type": "rule_card", "label": "规则",
                  "room_type_id": self.twin, "rule_version_id": rv,
                  "essential": True, "channels": geos()},
                 {"type": "window_card", "label": "天井",
                  "room_type_id": self.twin, "feature_id": fv,
                  "essential": False, "channels": geos()}]}]}
        return aid, rv, fv, tl

    # ------------------------------------------------------------ scope

    def test_shared_lobby_cannot_be_private_room_space(self):
        tl = self.good_timeline()
        tl["items"][0]["scope"] = "private"
        tl["items"][0]["room_type_id"] = self.king
        st, d = call("POST", "/api/projects", self.bob,
                     {"property_id": self.pid, "title": "bad", "timeline": tl})
        self.assertEqual(st, 422)
        codes = {i["code"] for i in d["issues"]}
        self.assertIn("SHARED_ASSET_MARKED_PRIVATE", codes)

    def test_asset_create_enforces_scope_binding(self):
        sha = store.get_asset_version(self.lobby, 1)["sha256"]
        # shared asset with a room type -> rejected
        st, d = call("POST", "/api/assets", self.bob, {
            "property_id": self.pid, "kind": "video", "title": "x",
            "scope": "shared", "room_type_id": self.king, "sha256": sha,
            "size_bytes": 1, "source_uri": "u"})
        self.assertEqual(st, 422, d)
        self.assertEqual(d["error"], "SHARED_HAS_ROOM")
        # private asset without room -> rejected
        st, d = call("POST", "/api/assets", self.bob, {
            "property_id": self.pid, "kind": "video", "title": "x",
            "scope": "private", "sha256": sha, "size_bytes": 1, "source_uri": "u"})
        self.assertEqual(st, 422)
        self.assertEqual(d["error"], "PRIVATE_NEEDS_ROOM")

    def test_private_asset_cannot_attach_to_other_room(self):
        tl = self.good_timeline()
        tl["items"][1]["room_type_id"] = self.twin
        st, d = call("POST", "/api/projects", self.bob,
                     {"property_id": self.pid, "title": "bad", "timeline": tl})
        self.assertEqual(st, 422)
        self.assertTrue(any(i["code"] == "ROOM_MISMATCH" for i in d["issues"]))

    # ----------------------------------------------------- safe areas

    def test_vertical_crop_clips_essential_prompt(self):
        tl = self.good_timeline()
        # move the fire-exit prompt to the far left edge of the 16:9 canvas:
        # visible in landscape, fully cropped in 9:16
        tl["items"][0]["embedded_prompts"][0].update(
            {"x": 0.02, "y": 0.1, "w": 0.14, "h": 0.08})
        report = domain.dependency_report(
            {"property_id": self.pid}, tl, "2026-10-03", _S())
        bad = [i for i in report["issues"]
               if i.get("channel") == "douyin"
               and i["code"] == "CROP_CLIPS_ESSENTIAL"]
        self.assertTrue(bad, "竖版裁切遮掉必要提示必须报错")

    def test_overlay_needs_per_channel_geometry(self):
        tl = self.good_timeline()
        tl["channels"].append("kuaishou")
        # card has no kuaishou geometry -> must block, cannot assume
        # landscape position/thumbnail is fine
        st, d = call("POST", "/api/projects", self.bob,
                     {"property_id": self.pid, "title": "bad", "timeline": tl})
        self.assertEqual(st, 422)
        self.assertTrue(any(i["code"] == "OVERLAY_NO_CHANNEL_GEOMETRY"
                            and "kuaishou" in i["detail"] for i in d["issues"]))

    def test_thumbnail_only_is_not_publishable(self):
        tl = self.good_timeline()
        tl["items"].append({
            "clip_id": "th", "asset_id": self.thumb, "asset_version": 1,
            "mode": "reference", "scope": "shared", "room_type_id": None,
            "duration": 2, "caption": "", "embedded_prompts": [],
            "overlays": [{"type": "caption", "label": "字幕"}]})
        pid = self.make_project(tl)
        st, d = call("POST", f"/api/projects/{pid}/publish", self.bob, {})
        self.assertEqual(st, 422)
        self.assertTrue(any(i["code"] == "THUMBNAIL_USED_AS_MEDIA"
                            for i in d["checks"]["issues"]))

    # -------------------------------------------------- rule revisions

    def test_rule_revision_blocks_stale_card(self):
        aid, rv, fv, tl = self._fresh_rule_timeline()
        pid = self.make_project(tl)
        st, _ = call("POST", f"/api/projects/{pid}/publish", self.bob, {})
        self.assertEqual(st, 200)
        # new revision effective 10-04 makes the pinned card stale then
        store.add_rule_revision(self.twin, {"check_in": "16:00", "check_out": "10:30",
                                            "pets": "no", "smoking": "no"},
                                "2026-10-04", None, self.alice)
        st, d = call("POST", f"/api/projects/{pid}/publish", self.bob,
                     {"as_of": "2026-10-04"})
        self.assertEqual(st, 422)
        self.assertTrue(any(i["code"] == "RULE_REVISION_STALE"
                            for i in d["checks"]["issues"]))
        # at 2026-09-15 the pinned revision (effective 2026-01-01) is fine;
        # a card pinned to a FUTURE revision would be not-effective
        st, d = call("GET", f"/api/projects/{pid}/checks?as_of=2026-09-15",
                     self.bob)
        self.assertTrue(all(i["code"] != "RULE_NOT_EFFECTIVE"
                            for i in d["issues"]))
        future = store.add_rule_revision(
            self.twin, {"check_in": "14:00", "check_out": "12:00",
                        "pets": "no", "smoking": "no"}, "2027-01-01",
            None, self.alice)
        tl["items"][0]["overlays"][1]["rule_version_id"] = future
        pid2 = self.make_project(tl)
        st, d = call("GET", f"/api/projects/{pid2}/checks?as_of=2026-09-15",
                     self.bob)
        self.assertTrue(any(i["code"] == "RULE_NOT_EFFECTIVE"
                            for i in d["issues"]))

    def test_expired_feature_window_blocks(self):
        aid, rv, fv, tl = self._fresh_rule_timeline()
        season = store.add_feature(self.twin, "facility", "季节性泡池",
                                   "2026-04-01", "2026-08-31", "")
        tl["items"][0]["overlays"].append({
            "type": "facility_card", "label": "泡池",
            "room_type_id": self.twin, "feature_id": season,
            "essential": True,
            "channels": {ch: {"x": 0.1, "y": 0.5, "w": 0.5, "h": 0.1}
                         for ch in tl["channels"]}})
        pid = self.make_project(tl)
        st, d = call("GET", f"/api/projects/{pid}/checks?as_of=2026-09-15",
                     self.bob)
        self.assertTrue(any(i["code"] == "FEATURE_INVALID" for i in d["issues"]))

    # --------------------------------------------- photo source failure

    def test_dead_photo_source_blocks_publish(self):
        pid = self.make_project()
        store.set_source_status(self.room, 1, "unreachable")
        st, d = call("POST", f"/api/projects/{pid}/publish", self.bob, {})
        self.assertEqual(st, 422)
        self.assertTrue(any(i["code"] == "PHOTO_SOURCE_UNREACHABLE"
                            for i in d["checks"]["issues"]))

    # --------------------------------------------------------- conflict

    def test_two_editors_same_clip_conflict(self):
        pid = self.make_project()
        st, d = call("GET", f"/api/projects/{pid}", self.bob)
        base = d["latest_version"]
        tl_bob = d["timeline"]
        tl_eve = json.loads(json.dumps(tl_bob))
        # bob saves first
        tl_bob["items"][0]["caption"] = "甲改的字幕"
        st, d = call("POST", f"/api/projects/{pid}/timeline", self.bob,
                     {"base_version": base, "timeline": tl_bob})
        self.assertEqual(st, 200)
        # evelyn still holds base=1 -> 409 with current version
        tl_eve["items"][0]["caption"] = "乙改的字幕"
        st, d = call("POST", f"/api/projects/{pid}/timeline", self.evelyn,
                     {"base_version": base, "timeline": tl_eve})
        self.assertEqual(st, 409)
        self.assertEqual(d["current_version"], 2)
        # after refreshing to v2 evelyn can save, nothing silently lost
        st, d2 = call("POST", f"/api/projects/{pid}/timeline", self.evelyn,
                      {"base_version": 2, "timeline": tl_eve})
        self.assertEqual(st, 200)
        self.assertEqual(d2["version"], 3)

    # ---------------------------------------------------------- uploads

    def test_chunked_upload_retry_and_complete(self):
        data = b"".join(bytes([i % 256]) * 1000 for i in range(20))  # 20KB
        chunk = 7000
        st, d = call("POST", "/api/uploads", self.bob,
                     {"filename": "a.bin", "total_size": len(data),
                      "chunk_size": chunk})
        self.assertEqual(st, 200)
        uid = d["upload_id"]
        n = (len(data) + chunk - 1) // chunk

        def send(i):
            return call("PUT", f"/api/uploads/{uid}/chunks/{i}", self.bob,
                        raw=data[i * chunk:(i + 1) * chunk],
                        ctype="application/octet-stream")

        st, _ = send(0)
        self.assertEqual(st, 200)
        st, _ = send(0)  # RETRY the same chunk -> idempotent
        self.assertEqual(st, 200)
        for i in range(1, n):
            st, _ = send(i)
            self.assertEqual(st, 200)
        # status endpoint supports resume ("how much got through?")
        st, s = call("GET", f"/api/uploads/{uid}", self.bob)
        self.assertEqual(s["received"], len(data))
        st, d = call("POST", f"/api/uploads/{uid}/complete", self.bob)
        self.assertEqual(st, 200, d)
        self.assertEqual(d["size_bytes"], len(data))
        # completing again is idempotent (retry after ambiguous response)
        st, d2 = call("POST", f"/api/uploads/{uid}/complete", self.bob)
        self.assertEqual(st, 200)
        self.assertEqual(d["sha256"], d2["sha256"])

    # ---------------------------------------------------- permission loss

    def test_resume_after_permission_revoked_is_403(self):
        pid = self.make_project(user=self.bob)
        # outsider was briefly added then removed
        store.add_member(self.pid, self.outsider, "viewer")
        st, d = call("POST", f"/api/projects/{pid}/resume", self.outsider)
        self.assertEqual(st, 200)
        from backend.db import get_conn
        with get_conn() as c:
            c.execute("DELETE FROM property_members WHERE property_id=? AND user_id=?",
                      (self.pid, self.outsider))
        st, d = call("POST", f"/api/projects/{pid}/resume", self.outsider)
        self.assertEqual(st, 403)
        self.assertEqual(d["error"], "MEMBERSHIP_REQUIRED")

    def test_role_forbidden_for_viewer(self):
        store.add_member(self.pid, self.outsider, "viewer")
        st, d = call("POST", "/api/projects", self.outsider,
                     {"property_id": self.pid, "title": "x"})
        self.assertEqual(st, 403)

    # ------------------------------------------------- master/license

    def test_reference_vs_copy_under_master_update_and_withdrawal(self):
        aid, rv, fv, tl = self._fresh_rule_timeline()
        # Project A: clip COPIED in; baseline export.
        pid = self.make_project(tl)
        st, d = call("POST", f"/api/projects/{pid}/copy-clip", self.bob,
                     {"clip_id": "room"})
        self.assertEqual(st, 200)
        st, _ = call("POST", f"/api/projects/{pid}/publish", self.bob, {})
        self.assertEqual(st, 200)
        export_before = self._latest_export(pid)

        # master gets a NEW version: the copied clip must not move.
        new_sha, new_sz = put_bytes(b"INDEPENDENT-ROOM-V2" + uuid.uuid4().bytes)
        store.add_asset_version(aid, sha256=new_sha, size_bytes=new_sz,
                                source_uri="media://ind-v2", width=1920,
                                height=1080, duration=4)
        st, d = call("GET", f"/api/projects/{pid}/checks", self.bob)
        self.assertNotIn("MASTER_UPDATED", {i["code"] for i in d["issues"]})

        # Project B references the master -> update warning, non-blocking.
        _, _, _, tl2 = self._fresh_rule_timeline()
        ref_aid = tl2["items"][0]["asset_id"]
        # force v2 onto this separate asset
        v2, v2sz = put_bytes(b"INDEPENDENT-ROOM-V2B" + uuid.uuid4().bytes)
        store.add_asset_version(ref_aid, sha256=v2, size_bytes=v2sz,
                                source_uri="media://ind2-v2", width=1920,
                                height=1080, duration=4)
        ref_pid = self.make_project(tl2)
        st, d = call("GET", f"/api/projects/{ref_pid}/checks", self.bob)
        self.assertIn("MASTER_UPDATED", {i["code"] for i in d["issues"]})
        st, _ = call("POST", f"/api/projects/{ref_pid}/publish", self.bob, {})
        self.assertEqual(st, 200)

        # withdraw the pinned OLD license: new reference publishes block...
        store.withdraw_license(ref_aid, 1, "合作终止")
        st, d = call("POST", f"/api/projects/{ref_pid}/publish", self.bob, {})
        self.assertEqual(st, 422)
        self.assertTrue(any(i["code"] == "LICENSE_WITHDRAWN"
                            for i in d["checks"]["issues"]))
        # ...while the copied project's earlier export remains on record.
        st, after = call("GET", f"/api/projects/{pid}/exports", self.bob)
        self.assertTrue(any(e["id"] == export_before["id"]
                            for e in after["exports"]))

    def _latest_export(self, pid):
        st, d = call("GET", f"/api/projects/{pid}/exports", self.bob)
        return d["exports"][0]

    # ----------------------------------------------------- cursor/freeze

    def test_home_cursor_pins_exact_version_and_old_export_frozen(self):
        aid, rv, fv, tl = self._fresh_rule_timeline()
        pid = self.make_project(tl)
        st, _ = call("POST", f"/api/projects/{pid}/publish", self.bob, {})
        self.assertEqual(st, 200)
        st, ex = call("GET", f"/api/projects/{pid}/exports", self.bob)
        eid = ex["exports"][0]["id"]
        st, e = call("GET", f"/api/exports/{eid}", self.bob)
        self.assertEqual(st, 200)
        frozen = [o["frozen_rule"]["check_in"] for it in e["snapshot_json"]["items"]
                  for o in it["overlays"] if o["type"] == "rule_card"][0]
        self.assertEqual(frozen, "14:00")

        # current rule text changes after the export
        store.add_rule_revision(self.twin, {"check_in": "16:00", "check_out": "10:00",
                                            "pets": "no", "smoking": "no"},
                                "2026-10-04", None, self.alice)
        st, e2 = call("GET", f"/api/exports/{eid}", self.bob)
        again = [o["frozen_rule"]["check_in"] for it in e2["snapshot_json"]["items"]
                 for o in it["overlays"] if o["type"] == "rule_card"][0]
        self.assertEqual(again, "14:00",
                         "旧导出必须保留制作时规则，不套用当前描述")

        st, home = call("GET", "/api/home", self.bob)
        row = [p for p in home["projects"] if p["project_id"] == pid][0]
        self.assertIsNotNone(row["cursor_version"])

    def test_unsupported_recommendation_blocked(self):
        tl = self.good_timeline()
        tl["items"][1]["overlays"].append({
            "type": "recommendation", "label": "附近网红咖啡店（随手写的）",
            "essential": False,
            "channels": {ch: {"x": 0.1, "y": 0.6, "w": 0.5, "h": 0.08}
                         for ch in tl["channels"]}})
        st, d = call("POST", "/api/projects", self.bob,
                     {"property_id": self.pid, "title": "bad", "timeline": tl})
        self.assertEqual(st, 422)
        self.assertTrue(any(i["code"] == "RECOMMENDATION_UNSUPPORTED"
                            for i in d["issues"]))

    def test_worker_recheck_blocks_job_after_revocation(self):
        # queue a job (inline disabled), revoke, run -> failed
        old = app.run_jobs_inline
        app.run_jobs_inline = False
        try:
            pid = self.make_project()
            pv = store.latest_project_version(pid)
            job_id = store.enqueue_job(pid, pv["id"], self.bob)  # queued...
            store.withdraw_license(self.room, 1, "排队期间撤回")  # ...state changes
            st, d = call("POST", f"/api/jobs/{job_id}/run", self.alice, {})
            self.assertEqual(d["status"], "failed")
            st, job = call("GET", f"/api/jobs/{job_id}", self.bob)
            self.assertEqual(job["status"], "failed")
            self.assertIn("授权已撤回", job["error"])
        finally:
            app.run_jobs_inline = old


class _S:
    @staticmethod
    def blob_exists(sha):
        from backend.storage import blob_exists
        return blob_exists(sha)


if __name__ == "__main__":
    unittest.main(verbosity=2)
