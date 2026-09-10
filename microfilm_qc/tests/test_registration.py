"""补扫图像配准核对回归测试。

覆盖需求要点：
  1. 配准四方向/有限平移：同页 IoU 高、倒置能被自动选回、相邻页误装落低置信。
  2. 门禁：低于阈值/配准失败/无原图可比的条目不得批量接受。
  3. 单项强制接受：低置信/无原图必须填写理由，理由随条目保存。
  4. 撤销接受不丢失核对记录（reg_status/reg_detail/force_reason 保留）。
  5. 改绑后重新配准；批次 JSON / 决策 CSV 含配准列。
"""
import csv
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    import flask  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(
        os.path.dirname(ROOT), ".pyuser", "lib", "python3.11", "site-packages"))

from PIL import Image  # noqa: E402

import app  # noqa: E402
from qc_core.db import DB  # noqa: E402
from qc_core import register as regmod, sample_reel  # noqa: E402
import random  # noqa: E402


class RegistrationAlgorithmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = random.Random(20260910)
        cls.pages = {no: sample_reel.make_page(no, rng)
                     for no in range(1, sample_reel.N_BODY + 2)}

    def test_same_page_high_iou_and_identity_transform(self):
        reg = regmod.full_translation(regmod.register(self.pages[10], self.pages[10]))
        self.assertEqual(reg["rotation"], 0)
        self.assertEqual((reg["dx"], reg["dy"]), (0, 0))
        self.assertGreaterEqual(reg["iou"], 0.9)
        self.assertEqual(regmod.classify(reg), regmod.STATUS_OK)

    def test_upside_down_is_recovered(self):
        flipped = self.pages[10].rotate(180)
        reg = regmod.register(self.pages[10], flipped)
        self.assertEqual(reg["rotation"], 180)
        self.assertGreaterEqual(reg["iou"], 0.9)
        self.assertEqual(regmod.classify(reg), regmod.STATUS_OK)

    def test_adjacent_misload_is_low_confidence(self):
        reg = regmod.register(self.pages[10], self.pages[11])
        self.assertLess(reg["iou"], regmod.IOU_PASS)
        self.assertEqual(regmod.classify(reg), regmod.STATUS_LOW)

    def test_overcrop_flagged_by_unmatched_edge(self):
        w, h = self.pages[10].size
        d = int(min(w, h) * 0.2)
        cropped = self.pages[10].crop((d, d, w - d, h - d))
        reg = regmod.register(self.pages[10], cropped)
        self.assertGreater(reg["edge_frac"], regmod.EDGE_WARN)
        self.assertIn(regmod.classify(reg),
                      (regmod.STATUS_LOW, regmod.STATUS_FAILED))

    def test_manual_transform_is_evaluated_exactly_as_submitted(self):
        # 人工微调必须按用户提交的 rotation/dx/dy 评估，不能重新搜索覆盖位移。
        # 同页图像在明显偏移处 IoU 应随之下降，且返回位移必须等于提交值（换算取整）。
        dx_full, dy_full = 60, 40
        reg = regmod.register(self.pages[10], self.pages[10],
                              manual=(0, dx_full, dy_full))
        reg = regmod.full_translation(reg)
        self.assertFalse(reg["auto"])
        # 返回的原图像素位移即用户提交值
        self.assertEqual(reg["dx_full"], dx_full)
        self.assertEqual(reg["dy_full"], dy_full)
        # 偏移后不再是高重合（自动搜索若生效会把 IoU 拉回 ~1.0）
        self.assertLess(reg["iou"], 0.85)

    def test_manual_rotation_is_kept_without_search_override(self):
        # 同页图像旋转 180 后，人工坚持 0° 评估，结果应保持 0°（不自动选回 180°）
        flipped = self.pages[10].rotate(180)
        reg = regmod.register(self.pages[10], flipped, manual=(0, 0, 0))
        self.assertEqual(reg["rotation"], 0)
        self.assertFalse(reg["auto"])
        self.assertLess(reg["iou"], regmod.IOU_PASS)
        # 同一对图自动搜索能找到 180°
        auto = regmod.register(self.pages[10], flipped)
        self.assertEqual(auto["rotation"], 180)
        self.assertGreaterEqual(auto["iou"], 0.9)

    def test_manual_zero_transform_matches_auto_identity(self):
        reg = regmod.full_translation(
            regmod.register(self.pages[10], self.pages[10], manual=(0, 0, 0)))
        self.assertFalse(reg["auto"])
        self.assertEqual((reg["dx_full"], reg["dy_full"]), (0, 0))
        self.assertGreaterEqual(reg["iou"], 0.9)


class RegistrationFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for sub in ("frames", "rescan", "boundary", "exports"):
            os.makedirs(os.path.join(self.tmp, sub), exist_ok=True)
        app.DATA = self.tmp
        app.FRAMES_DIR = os.path.join(self.tmp, "frames")
        app.RESCAN_DIR = os.path.join(self.tmp, "rescan")
        app.BOUNDARY_DIR = os.path.join(self.tmp, "boundary")
        app.EXPORT_DIR = os.path.join(self.tmp, "exports")
        app.db = DB(os.path.join(self.tmp, "test.db"))
        app.app.config["TESTING"] = True
        self.c = app.app.test_client()
        r = self.c.post("/api/sample")
        self.assertEqual(r.status_code, 200)
        self.reel_id = r.get_json()["reel_id"]
        r = self.c.post("/api/reels/%d/sample-rescan" % self.reel_id)
        j = r.get_json()
        self.batch_id = j["batch_id"]
        self.detail = j["detail"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _item(self, frame_no):
        return next(x for x in self.detail["items"] if x["frame_no"] == str(frame_no))

    def _refresh(self):
        self.detail = self.c.get("/api/rescans/%d" % self.batch_id).get_json()

    def test_import_runs_registration(self):
        # No.20 是缺帧占位回填 -> 无原图可比
        it = self._item(20)
        self.assertEqual(it["reg"]["status"], regmod.STATUS_NO_ORIGINAL)
        # No.33 重拍替换，补扫图与旧的暗帧内容不一致 -> 低置信（需人工）
        it33 = self._item(33)
        self.assertIn(it33["reg"]["status"],
                      (regmod.STATUS_LOW, regmod.STATUS_FAILED))

    def test_no_original_requires_force_reason(self):
        it = self._item(20)
        r = self.c.post("/api/rescan-items/%d/accept" % it["id"], json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("强制接受理由", r.get_json()["error"])
        r = self.c.post("/api/rescan-items/%d/accept" % it["id"],
                        json={"force_reason": "原片缺帧，已人工核对补扫内容"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_low_confidence_requires_force_reason(self):
        it = self._item(33)
        if it["reg"]["status"] == regmod.STATUS_NO_ORIGINAL:
            self.skipTest("样例中 No.33 非低置信项")
        r = self.c.post("/api/rescan-items/%d/accept" % it["id"], json={})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/rescan-items/%d/accept" % it["id"],
                        json={"force_reason": "已逐像素核对，确属同一页"})
        self.assertEqual(r.status_code, 200)

    def test_batch_accept_skips_gated_items(self):
        r = self.c.post("/api/rescans/%d/accept-clean" % self.batch_id)
        self.assertEqual(r.status_code, 200)
        skipped = {s["frame_no"]: s["reg_status"] for s in r.get_json()["skipped"]}
        # 待处理项 20（无原图）/33/13（低置信）都必须被跳过
        self.assertIn("20", skipped)
        self.assertEqual(skipped["20"], regmod.STATUS_NO_ORIGINAL)
        for no in ("33", "13"):
            it = self._item(no)
            if it["status"] == "pending":
                self.assertIn(no, skipped)
        # 跳过后仍是待处理，未被接受
        self._refresh()
        for no in skipped:
            self.assertEqual(self._item(no)["status"], "pending")

    def _blank_registration(self, frame_no):
        """把某条目的配准核对结果清空（模拟未完成核对）。"""
        it = self._item(frame_no)
        app.db.run(
            "UPDATE rescan_items SET reg_status='', reg_detail='', reg_manual=0 WHERE id=?",
            (it["id"],))
        return it["id"]

    def test_empty_reg_status_skipped_by_batch_accept(self):
        # 选一个原本 ok 的待处理项，清空其核对状态
        item_id = self._blank_registration(13)
        r = self.c.post("/api/rescans/%d/accept-clean" % self.batch_id)
        self.assertEqual(r.status_code, 200)
        skipped = {s["item_id"]: s for s in r.get_json()["skipped"]}
        self.assertIn(item_id, skipped)
        self.assertEqual(skipped[item_id]["reg_status"], "")
        # 未被接受
        self._refresh()
        self.assertEqual(next(x for x in self.detail["items"] if x["id"] == item_id)["status"],
                         "pending")

    def test_empty_reg_status_requires_force_reason(self):
        item_id = self._blank_registration(13)
        r = self.c.post("/api/rescan-items/%d/accept" % item_id, json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("尚未完成图像配准核对", r.get_json()["error"])
        r = self.c.post("/api/rescan-items/%d/accept" % item_id,
                        json={"force_reason": "旧配准数据缺失，已人工逐项核对"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_manual_recompute_keeps_submitted_translation(self):
        it = self._item(33)  # 有原图可比对
        dx, dy = 50, -30
        r = self.c.post("/api/rescan-items/%d/registration" % it["id"],
                        json={"manual": True, "rotation": 0,
                              "dx_full": dx, "dy_full": dy})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        reg = r.get_json()["registration"]
        self.assertTrue(reg["auto"] is False)
        self.assertEqual(reg["dx_full"], dx)
        self.assertEqual(reg["dy_full"], dy)
        self.assertEqual(reg["rotation"], 0)
        # 落库后详情同样保存用户位移，且标记人工微调
        out = next(x for x in r.get_json()["detail"]["items"] if x["id"] == it["id"])
        self.assertEqual(out["reg"]["dx_full"], dx)
        self.assertEqual(out["reg"]["dy_full"], dy)
        self.assertTrue(out["reg"]["manual"])

    def test_manual_recompute_reads_public_dx_dy_fields(self):
        # 公开契约：manual=true 提交 rotation/dx/dy，配准层必须收到并持久化 (90, 17, -9)，
        # 不能静默丢成 (90, 0, 0)，也不能重新搜索覆盖位移。
        it = self._item(33)
        rotation, dx, dy = 90, 17, -9
        r = self.c.post("/api/rescan-items/%d/registration" % it["id"],
                        json={"manual": True, "rotation": rotation,
                              "dx": dx, "dy": dy})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        reg = r.get_json()["registration"]
        self.assertFalse(reg["auto"])
        self.assertEqual(reg["rotation"], rotation)
        self.assertEqual(reg["dx_full"], dx)
        self.assertEqual(reg["dy_full"], dy)
        # 落库后的详情（持久化）一致
        out = next(x for x in r.get_json()["detail"]["items"] if x["id"] == it["id"])
        self.assertEqual(out["reg"]["rotation"], rotation)
        self.assertEqual(out["reg"]["dx_full"], dx)
        self.assertEqual(out["reg"]["dy_full"], dy)
        self.assertTrue(out["reg"]["manual"])
        # 直接查库核对保存的是公开字段值，而非 0
        row = app.db.one(
            "SELECT reg_status, reg_detail, reg_manual FROM rescan_items WHERE id=?",
            (it["id"],))
        self.assertEqual(row["reg_manual"], 1)
        detail = json.loads(row["reg_detail"])
        self.assertEqual((detail["rotation"], detail["dx_full"], detail["dy_full"]),
                         (rotation, dx, dy))

    def test_manual_recompute_nonzero_translation_changes_metric(self):
        # 同条目在 0 位移给出基准 IoU；提交非零公开位移后必须反映该位移，
        # 证明 dx/dy 确实传到了配准层（而不是被忽略成 0）。
        it = self._item(33)
        base = self.c.post("/api/rescan-items/%d/registration" % it["id"],
                           json={"manual": True, "rotation": 0, "dx": 0, "dy": 0})
        base_iou = base.get_json()["registration"]["iou"]
        moved = self.c.post("/api/rescan-items/%d/registration" % it["id"],
                            json={"manual": True, "rotation": 0, "dx": 17, "dy": -9})
        moved_reg = moved.get_json()["registration"]
        self.assertEqual((moved_reg["dx_full"], moved_reg["dy_full"]), (17, -9))
        self.assertNotAlmostEqual(moved_reg["iou"], base_iou, places=3)

    def test_undo_accept_keeps_registration_record(self):
        it = self._item(20)
        reason = "撤销回归：理由不应丢失"
        self.assertEqual(
            self.c.post("/api/rescan-items/%d/accept" % it["id"],
                        json={"force_reason": reason}).status_code, 200)
        self.assertEqual(self.c.post("/api/reels/%d/undo" % self.reel_id).status_code, 200)
        self._refresh()
        back = self._item(20)
        self.assertEqual(back["status"], "pending")
        self.assertEqual(back["reg"]["status"], regmod.STATUS_NO_ORIGINAL)
        self.assertEqual(back["reg"]["force_reason"], reason)

    def test_rebind_reruns_registration(self):
        # 唯一内容文件指向不存在的 No.77 -> 拦截；改绑到占位 37A 应放行并重算配准
        rng = random.Random(246810)
        img = Image.new("L", (900, 1240), 232)
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        for _ in range(70):
            x, y = rng.randint(0, 760), rng.randint(0, 1160)
            d.rectangle([x, y, x + rng.randint(40, 220), y + rng.randint(4, 18)],
                        fill=rng.randint(20, 90))
        zbuf = io.BytesIO()
        import zipfile
        b = io.BytesIO(); img.convert("RGB").save(b, "JPEG", quality=90)
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("UNIQ_0077.jpg", b.getvalue())
        rows = [["reel_no", "frame_no", "filename", "note"],
                ["R2026-001", "77", "UNIQ_0077.jpg", "目标不存在待改绑"]]
        mb = io.StringIO(); w = csv.writer(mb); w.writerows(rows)
        r = self.c.post("/api/reels/%d/rescans/import" % self.reel_id,
                        data={"zip": (io.BytesIO(zbuf.getvalue()), "u.zip"),
                              "manifest": (io.BytesIO(mb.getvalue().encode("utf-8-sig")), "u.csv"),
                              "name": "改绑配准批次"},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        it = r.get_json()["detail"]["items"][0]
        self.assertEqual(it["status"], "blocked")
        r = self.c.post("/api/rescan-items/%d/rebind" % it["id"], json={"frame_no": "37A"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        out = next(x for x in r.get_json()["detail"]["items"] if x["id"] == it["id"])
        self.assertEqual(out["status"], "pending")
        self.assertEqual(out["reg"]["status"], regmod.STATUS_NO_ORIGINAL)

    def test_exports_contain_registration_columns(self):
        it = self._item(20)
        self.c.post("/api/rescan-items/%d/accept" % it["id"],
                    json={"force_reason": "导出测试理由"})
        csv_bytes = self.c.get(
            "/api/rescans/%d/export/decisions.csv" % self.batch_id).data.decode("utf-8-sig")
        header = next(csv.reader(io.StringIO(csv_bytes)))
        for col in ("配准状态", "结构相似度IoU", "未重合边缘", "强制接受理由"):
            self.assertIn(col, header)
        payload = self.c.get(
            "/api/rescans/%d/export/batch.json" % self.batch_id).get_json()
        row = next(x for x in payload["items"] if x["frame_no"] == "20")
        self.assertEqual(row["reg"]["force_reason"], "导出测试理由")

    def test_registration_visual_endpoints(self):
        it = self._item(33)  # 有原图可叠
        for fmt in ("overlay", "diff", "new"):
            r = self.c.get("/api/rescan-items/%d/registration.%s?w=500" % (it["id"], fmt))
            self.assertEqual(r.status_code, 200, fmt)
            self.assertEqual(r.mimetype, "image/jpeg")
        # 无原图条目不出叠加图
        it20 = self._item(20)
        r = self.c.get("/api/rescan-items/%d/registration.overlay" % it20["id"])
        self.assertEqual(r.status_code, 404)


class ViewerRebindNodeTests(unittest.TestCase):
    """换卡后查看器重绑/空状态门禁的原生 JS 回归（用 node 执行，无 node 则跳过）。"""

    @classmethod
    def setUpClass(cls):
        import shutil
        cls.node = shutil.which("node")
        cls.script = os.path.join(ROOT, "tests", "test_rescan_viewer.js")

    def test_rescan_viewer_js(self):
        if not self.node:
            self.skipTest("未安装 node，跳过前端查看器重绑回归")
        import subprocess
        p = subprocess.run([self.node, self.script], capture_output=True, text=True)
        if p.returncode != 0:
            self.fail("前端查看器回归失败：\n" + p.stdout + "\n" + p.stderr)


if __name__ == "__main__":
    unittest.main()
