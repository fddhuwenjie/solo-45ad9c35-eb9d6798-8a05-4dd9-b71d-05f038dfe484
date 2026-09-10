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

    def test_manual_nudge_recomputes_nearby(self):
        # 给同页一个偏移初值，人工微调应在附近精搜回最优
        reg = regmod.register(self.pages[10], self.pages[10], manual=(0, 10, 10))
        self.assertFalse(reg["auto"])
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


if __name__ == "__main__":
    unittest.main()
