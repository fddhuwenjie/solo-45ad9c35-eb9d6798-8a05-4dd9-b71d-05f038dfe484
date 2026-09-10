"""补扫回填模块回归测试。

覆盖两处已复现缺陷：
  1. 逐项拒绝混用 batch_id 与 reel_id：同卷第二批次（batch_id=2、reel_id=1）拒绝条目
     必须成功、撤销状态记到正确的卷，撤销后条目恢复拒绝前状态。
  2. 因“同一文件重复占用”被拦截的文件，不能仅靠改绑变成 pending 后再被另一帧接受。
"""
import csv
import io
import os
import random
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    import flask  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(
        os.path.dirname(ROOT), ".pyuser", "lib", "python3.11", "site-packages"))

from PIL import Image, ImageDraw  # noqa: E402

import app  # noqa: E402
from qc_core.db import DB  # noqa: E402

REEL_NO = "R2026-001"


def custom_rescan_package(frame_no, filename, seed=987654321):
    """生成一份内容与现有帧明显不同的补扫 ZIP + 回填清单（单条目）。"""
    rng = random.Random(seed)
    img = Image.new("L", (900, 1240), 232)
    d = ImageDraw.Draw(img)
    for _ in range(60):
        x, y = rng.randint(0, 760), rng.randint(0, 1160)
        d.rectangle([x, y, x + rng.randint(40, 220), y + rng.randint(4, 18)],
                    fill=rng.randint(20, 90))
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        b = io.BytesIO()
        img.convert("RGB").save(b, "JPEG", quality=90)
        zf.writestr(filename, b.getvalue())
    cbuf = io.StringIO()
    w = csv.writer(cbuf)
    w.writerow(["reel_no", "frame_no", "filename", "note"])
    w.writerow([REEL_NO, str(frame_no), filename, "第二批次自定义补扫"])
    return zbuf.getvalue(), cbuf.getvalue().encode("utf-8-sig")


class RescanRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.frames_dir = os.path.join(self.tmp, "frames")
        self.rescan_dir = os.path.join(self.tmp, "rescan")
        self.export_dir = os.path.join(self.tmp, "exports")
        for d in (self.frames_dir, self.rescan_dir, self.export_dir):
            os.makedirs(d, exist_ok=True)
        app.DATA = self.tmp
        app.FRAMES_DIR = self.frames_dir
        app.RESCAN_DIR = self.rescan_dir
        app.EXPORT_DIR = self.export_dir
        app.db = DB(os.path.join(self.tmp, "test.db"))
        app.app.config["TESTING"] = True
        self.c = app.app.test_client()
        r = self.c.post("/api/sample")
        self.assertEqual(r.status_code, 200)
        self.reel_id = r.get_json()["reel_id"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _create_sample_batch(self):
        r = self.c.post("/api/reels/%d/sample-rescan" % self.reel_id)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        return j["batch_id"], j["detail"]

    def _item(self, detail, frame_no=None, filename_endswith=None):
        for it in detail["items"]:
            if frame_no is not None and it["frame_no"] != str(frame_no):
                continue
            if filename_endswith and not it["filename"].endswith(filename_endswith):
                continue
            return it
        raise AssertionError("条目不存在 frame_no=%s endswith=%s" % (frame_no, filename_endswith))

    # ---- 缺陷 1：拒绝混用 batch_id / reel_id ----

    def test_reject_second_batch_with_unequal_ids_and_undo(self):
        # 第一批：batch_id=1
        b1, _ = self._create_sample_batch()
        # 第二批：自定义包，指向仍处于重拍状态的 No.13（内容与所有现有文件不同）
        zbytes, mbytes = custom_rescan_package(13, "CUSTOM_0013_retake.jpg")
        r = self.c.post(
            "/api/reels/%d/rescans/import" % self.reel_id,
            data={"zip": (io.BytesIO(zbytes), "b2.zip"),
                  "manifest": (io.BytesIO(mbytes), "b2.csv"),
                  "name": "第二批次"},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        b2 = j["batch_id"]
        item = j["detail"]["items"][0]

        # 关键前置：批次 id 与卷 id 不相等（reel_id=1，batch_id=2）
        self.assertEqual(self.reel_id, 1)
        self.assertEqual(b1, 1)
        self.assertEqual(b2, 2)
        self.assertNotEqual(b2, self.reel_id)
        self.assertEqual(item["status"], "pending")

        # 拒绝必须成功（旧实现把 batch_id 当 reel_id，导致 state(2) 404）
        r = self.c.post("/api/rescan-items/%d/reject" % item["id"],
                        json={"note": "不需要"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rj = r.get_json()
        out_item = self._item(rj["detail"], frame_no="13",
                              filename_endswith="CUSTOM_0013_retake.jpg")
        self.assertEqual(out_item["status"], "rejected")
        # 返回的 state 必须属于 reel 1
        self.assertEqual(rj["state"]["reel"]["id"], self.reel_id)

        # 撤销所需状态必须记在 reel_id=1 的修订上，且不能在 reel 2 留任何记录
        rev = app.db.one(
            "SELECT * FROM revisions WHERE reel_id=? ORDER BY id DESC LIMIT 1",
            (self.reel_id,))
        self.assertIsNotNone(rev)
        self.assertIn("拒绝补扫条目", rev["action"])
        self.assertTrue(rev["extra"])
        self.assertIn(str(item["id"]), rev["extra"])
        self.assertEqual(
            app.db.one("SELECT COUNT(*) c FROM revisions WHERE reel_id=?",
                       (b2,))["c"], 0)

        # 撤销：条目恢复拒绝前状态
        r = self.c.post("/api/reels/%d/undo" % self.reel_id)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("拒绝补扫条目", r.get_json()["undone"])
        d = self.c.get("/api/rescans/%d" % b2).get_json()
        back = self._item(d, frame_no="13",
                          filename_endswith="CUSTOM_0013_retake.jpg")
        self.assertEqual(back["status"], "pending")
        self.assertEqual(back["decision_note"], "")

    # ---- 缺陷 2：重复文件改绑绕过占用校验 ----

    def test_duplicate_file_cannot_become_pending_via_rebind(self):
        _bid, detail = self._create_sample_batch()
        state = self.c.get("/api/reels/%d/state" % self.reel_id).get_json()
        target = next(f for f in state["frames"] if f["frame_no"] == "37A")

        # No.12 的补扫文件与 No.12 当前有效图字节相同 -> 以“同一文件重复占用”拦截
        dup = self._item(detail, frame_no="12",
                         filename_endswith="_0012_dup.tif")
        self.assertEqual(dup["status"], "blocked")
        self.assertIn("同一文件重复占用", dup["block_reason"])

        rev_before = app.db.one(
            "SELECT COUNT(*) c FROM revisions WHERE reel_id=?",
            (self.reel_id,))["c"]

        # 改绑到合法占位帧也必须失败，不能借改绑洗成 pending
        r = self.c.post("/api/rescan-items/%d/rebind" % dup["id"],
                        json={"frame_no": "37A"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("同一文件重复占用", r.get_json()["error"])

        # 条目维持拦截原状
        d = self.c.get("/api/rescans/1").get_json()
        after = self._item(d, frame_no="12", filename_endswith="_0012_dup.tif")
        self.assertEqual(after["status"], "blocked")
        self.assertIn("同一文件重复占用", after["block_reason"])
        self.assertNotEqual(after["target_frame_id"], target["id"])

        # 失败的改绑不能留下修订，也不能直接接受
        rev_after = app.db.one(
            "SELECT COUNT(*) c FROM revisions WHERE reel_id=?",
            (self.reel_id,))["c"]
        self.assertEqual(rev_before, rev_after)
        r = self.c.post("/api/rescan-items/%d/accept" % dup["id"], json={})
        self.assertEqual(r.status_code, 400)

    def test_non_duplicate_blocked_item_can_still_rebind(self):
        """守卫用例：非文件占用原因（目标不存在）被拦截的唯一项，改绑仍应放行。"""
        self._create_sample_batch()
        # 第二批次：唯一内容文件指向不存在的 No.77 -> 目标不存在拦截
        zbytes, mbytes = custom_rescan_package(77, "UNIQUE_0077_extra.jpg",
                                               seed=1357911)
        r = self.c.post(
            "/api/reels/%d/rescans/import" % self.reel_id,
            data={"zip": (io.BytesIO(zbytes), "u.zip"),
                  "manifest": (io.BytesIO(mbytes), "u.csv"),
                  "name": "守卫批次"},
            content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        item = r.get_json()["detail"]["items"][0]
        self.assertEqual(item["status"], "blocked")
        self.assertIn("目标不存在", item["block_reason"])

        # 文件本身无占用问题，改绑到合法占位帧应放行
        r = self.c.post("/api/rescan-items/%d/rebind" % item["id"],
                        json={"frame_no": "37A"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        out = next(x for x in r.get_json()["detail"]["items"] if x["id"] == item["id"])
        self.assertEqual(out["status"], "pending")
        self.assertEqual(out["frame_no"], "37A")
        self.assertIsNotNone(out.get("new"))


if __name__ == "__main__":
    unittest.main()
