"""帧边界复核模块回归测试。

覆盖：
  1. 候选检测：演示卷中粘连图（No.7）应报拆分、误切半幅（No.17/17b）应报合并；
  2. 确认拆分：第 2 段就地填充其后的缺帧占位（No.8），原图/来源区间保留，可撤销恢复；
  3. 确认合并：两半上下拼回正常画幅，重新编号，可撤销恢复；
  4. 拦截：零宽片段、交叉切线（混向切线）、非相邻合并、重复占用、占位帧拆分、跨卷；
  5. 局部告警重算与导出（变更 JSON / 前后对照图 / 移交清单 provenance）。
"""
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

import app  # noqa: E402
from qc_core.db import DB  # noqa: E402
from qc_core import boundary  # noqa: E402
from PIL import Image  # noqa: E402


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for d in ("frames", "rescan", "boundary", "exports"):
            os.makedirs(os.path.join(self.tmp, d), exist_ok=True)
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
        self.rid = r.get_json()["reel_id"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self):
        return self.c.get("/api/reels/%d/state" % self.rid).get_json()

    def _frame(self, state, no):
        return next(f for f in state["frames"] if f["frame_no"] == no)

    def _candidates(self):
        return self.c.get("/api/reels/%d/boundary/candidates" % self.rid).get_json()

    # ---- 候选检测 ----
    def test_candidates_detect_glued_and_miscut(self):
        cand = self._candidates()
        split_nos = {s["frame_no"] for s in cand["splits"]}
        self.assertIn("7", split_nos)  # 粘连宽图
        glue = next(s for s in cand["splits"] if s["frame_no"] == "7")
        self.assertGreaterEqual(glue["confidence"], 0.7)
        self.assertEqual(glue["axis"], "x")
        self.assertTrue(any("亮度谷" in r or "投影" in r for r in glue["reasons"]))
        # 旋转页/误切半幅边缘谷不应产生拆分假候选
        self.assertNotIn("17", split_nos)
        self.assertNotIn("17b", split_nos)

        merge_pairs = {tuple(m["frame_nos"]) for m in cand["merges"]}
        self.assertIn(("17", "17b"), merge_pairs)
        m = next(m for m in cand["merges"] if tuple(m["frame_nos"]) == ("17", "17b"))
        self.assertEqual(m["layout"], "v")
        self.assertGreaterEqual(m["confidence"], 0.7)

    # ---- 拆分 ----
    def test_split_fills_placeholder_and_undo(self):
        st = self._state()
        f7, f8 = self._frame(st, "7"), self._frame(st, "8")
        self.assertTrue(f8["placeholder"])
        cand = self._candidates()
        cut = next(s for s in cand["splits"] if s["frame_no"] == "7")["cuts"]

        r = self.c.post("/api/frame/%d/boundary/split" % f7["id"],
                        json={"cuts": cut, "reason": "列亮度谷确认粘连"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertEqual(len(j["outputs"]), 2)
        st2 = j["state"]
        n7, n8 = self._frame(st2, "7"), self._frame(st2, "8")
        self.assertFalse(n8["placeholder"])           # 占位被第二片段填充
        self.assertAlmostEqual(n7["width"] + n8["width"], f7["width"], delta=6)
        self.assertEqual(n7["boundary"]["kind"], "crop")
        self.assertEqual(n7["boundary"]["region_count"], 2)
        self.assertTrue(n7["filename"].endswith(".tif"))
        # 操作批次与修订
        self.assertEqual(len(st2["boundary_ops"]), 1)
        self.assertTrue(st2["can_undo"])
        # 生成文件落盘，原图保留
        self.assertTrue(os.path.exists(n7["stored_path" ] if False else
                        app.db.one("SELECT stored_path p FROM frames WHERE id=?",
                                   (n7["id"],))["p"]))
        glued_path = os.path.join(self.tmp, "frames", str(self.rid),
                                  "R2026-001_0007_glued.tif")
        self.assertTrue(os.path.exists(glued_path))

        # 撤销：恢复粘连图、占位帧、边界与编号
        u = self.c.post("/api/reels/%d/undo" % self.rid)
        self.assertEqual(u.status_code, 200)
        st3 = u.get_json()["state"]
        f7b, f8b = self._frame(st3, "7"), self._frame(st3, "8")
        self.assertEqual(f7b["width"], f7["width"])
        self.assertTrue(f8b["placeholder"])
        self.assertEqual(len(st3["boundary_ops"]), 0)
        bdir = os.path.join(self.tmp, "boundary", str(self.rid))
        self.assertEqual(os.listdir(bdir), [])

    def test_split_inserts_new_frame_when_no_placeholder(self):
        # 没有后续占位时，拆分应新增一帧并整体重新编号（用 37A 之前的 No.36 不合适，
        # 直接对一张正常帧加切线也可；选 No.9）
        st = self._state()
        f9 = self._frame(st, "9")
        r = self.c.post("/api/frame/%d/boundary/split" % f9["id"],
                        json={"cuts": [{"axis": "x", "pos": 450}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        st2 = r.get_json()["state"]
        self.assertEqual(len(st2["frames"]), len(st["frames"]) + 1)
        # No.9 之后的数字帧整体 +1：原 No.10 现在应为 No.11
        pos9 = self._frame(st2, "9")["position"]
        self.assertEqual(st2["frames"][pos9 + 1]["frame_no"], "10")
        self.assertEqual(self._frame(st2, "11")["filename"],
                         next(f for f in st["frames"] if f["frame_no"] == "10")["filename"])

    # ---- 拦截 ----
    def test_split_intercepts_zero_width_and_crossing(self):
        st = self._state()
        f7 = self._frame(st, "7")
        rev_before = len(app.db.q("SELECT 1 FROM revisions WHERE reel_id=?", (self.rid,)))
        for cuts, key in [
            ([{"axis": "x", "pos": 12}], "零宽"),
            ([{"axis": "x", "pos": 900}, {"axis": "y", "pos": 600}], "交叉"),
            ([{"axis": "x", "pos": 99999}], "超出"),
        ]:
            r = self.c.post("/api/frame/%d/boundary/split" % f7["id"], json={"cuts": cuts})
            self.assertEqual(r.status_code, 400, cuts)
            self.assertIn(key, r.get_json()["error"])
        # 被拦截不能留下修订或操作批次
        self.assertEqual(len(app.db.q("SELECT 1 FROM revisions WHERE reel_id=?", (self.rid,))),
                         rev_before)
        self.assertEqual(app.db.one("SELECT COUNT(*) c FROM boundary_ops")["c"], 0)

    def test_placeholder_and_cross_reel_blocked(self):
        st = self._state()
        f8 = self._frame(st, "8")
        r = self.c.post("/api/frame/%d/boundary/split" % f8["id"],
                        json={"cuts": [{"axis": "x", "pos": 100}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("占位", r.get_json()["error"])
        # 跨卷：不存在的卷
        r = self.c.post("/api/reels/999/boundary/merge",
                        json={"frame_ids": [f8["id"], f8["id"]]})
        self.assertEqual(r.status_code, 404)

    def test_merge_intercepts_non_adjacent_and_duplicate(self):
        st = self._state()
        ids = {no: self._frame(st, no)["id"] for no in ("17", "17b", "18")}
        n_before = len(st["frames"])
        # 非相邻（17 与 18 之间隔着 17b）
        r = self.c.post("/api/reels/%d/boundary/merge" % self.rid,
                        json={"frame_ids": [ids["17"], ids["18"]]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("非相邻", r.get_json()["error"])
        # 乱序
        r = self.c.post("/api/reels/%d/boundary/merge" % self.rid,
                        json={"frame_ids": [ids["17b"], ids["17"]]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("顺序", r.get_json()["error"])
        # 重复占用（同一帧选两次）
        r = self.c.post("/api/reels/%d/boundary/merge" % self.rid,
                        json={"frame_ids": [ids["17"], ids["17"]]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("重复", r.get_json()["error"])
        # 全部拦截，帧数不变、无修订残留
        self.assertEqual(len(self._state()["frames"]), n_before)

    # ---- 合并主流程 + 撤销 ----
    def test_merge_miscut_and_undo(self):
        st = self._state()
        ids = [self._frame(st, no)["id"] for no in ("17", "17b")]
        r = self.c.post("/api/reels/%d/boundary/merge" % self.rid,
                        json={"frame_ids": ids, "reason": "误切两半"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        st2 = r.get_json()["state"]
        self.assertEqual(len(st2["frames"]), len(st["frames"]) - 1)
        out = self._frame(st2, "17")
        self.assertAlmostEqual(out["width"] / out["height"], 900 / 1240, delta=0.05)
        self.assertEqual(out["boundary"]["kind"], "stitch")
        self.assertEqual(out["boundary"]["region_layout"], "v")
        # 17b 不再存在
        self.assertIsNone(next((f for f in st2["frames"] if f["frame_no"] == "17b"), None))
        # 原图文件保留
        self.assertTrue(os.path.exists(os.path.join(
            self.tmp, "frames", str(self.rid), "R2026-001_0017a.jpg")))

        u = self.c.post("/api/reels/%d/undo" % self.rid)
        self.assertEqual(u.status_code, 200)
        st3 = u.get_json()["state"]
        self.assertEqual(len(st3["frames"]), len(st["frames"]))
        self.assertIsNotNone(next((f for f in st3["frames"] if f["frame_no"] == "17b"), None))
        self.assertEqual(self._frame(st3, "17")["width"], 900)

    # ---- 预览 ----
    def test_previews(self):
        st = self._state()
        f7 = self._frame(st, "7")
        r = self.c.get("/api/frame/%d/boundary/preview?cut=x:923&w=800" % f7["id"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/jpeg")
        Image.open(io.BytesIO(r.data)).verify()
        # 非法预览（零宽也应能给出 400）
        r = self.c.get("/api/frame/%d/boundary/preview?cut=x:2" % f7["id"])
        self.assertEqual(r.status_code, 400)
        ids = [self._frame(st, no)["id"] for no in ("17", "17b")]
        r = self.c.get("/api/reels/%d/boundary/merge-preview?ids=%d,%d"
                       % (self.rid, ids[0], ids[1]))
        self.assertEqual(r.status_code, 200)
        Image.open(io.BytesIO(r.data)).verify()

    # ---- 导出 ----
    def test_exports(self):
        st = self._state()
        ids = [self._frame(st, no)["id"] for no in ("17", "17b")]
        r = self.c.post("/api/reels/%d/boundary/merge" % self.rid,
                        json={"frame_ids": ids, "reason": "误切"})
        op_id = r.get_json()["op_id"]

        r = self.c.get("/api/reels/%d/boundary/export/changes.json" % self.rid)
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["op_count"], 1)
        op = payload["operations"][0]
        self.assertEqual(op["type"], "merge")
        self.assertEqual(op["layout"], "v")
        self.assertEqual([o["frame_no"] for o in op["outputs"]], ["17"])

        r = self.c.get("/api/boundary-ops/%d/comparison.png" % op_id)
        self.assertEqual(r.status_code, 200)
        Image.open(io.BytesIO(r.data)).verify()

        man = self.c.get("/api/reels/%d/export/manifest.json" % self.rid).get_json()
        row = next(x for x in man["frames"] if x["frame_no"] == "17")
        self.assertEqual(row["current_source"], "边界拆分/合并")
        self.assertIn("合并", row["provenance"])

    # ---- 链式：拆分后再拆分，撤销逐层恢复 ----
    def test_chained_split_undo(self):
        st = self._state()
        f7 = self._frame(st, "7")
        r = self.c.post("/api/frame/%d/boundary/split" % f7["id"],
                        json={"cuts": [{"axis": "x", "pos": 923}]})
        self.assertEqual(r.status_code, 200)
        st2 = r.get_json()["state"]
        f7b = self._frame(st2, "7")
        # 对拆分后的第 1 段再切一刀
        r = self.c.post("/api/frame/%d/boundary/split" % f7b["id"],
                        json={"cuts": [{"axis": "x", "pos": 460}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        st3 = r.get_json()["state"]
        self.assertEqual(len(st3["boundary_ops"]), 2)
        # 撤销第二次拆分：回到两段状态
        u = self.c.post("/api/reels/%d/undo" % self.rid).get_json()
        st4 = u["state"]
        self.assertEqual(len(st4["boundary_ops"]), 1)
        self.assertEqual(self._frame(st4, "7")["width"], 923)
        # 撤销第一次：恢复粘连
        u = self.c.post("/api/reels/%d/undo" % self.rid).get_json()
        st5 = u["state"]
        self.assertEqual(len(st5["boundary_ops"]), 0)
        self.assertEqual(self._frame(st5, "7")["width"], f7["width"])
        self.assertTrue(self._frame(st5, "8")["placeholder"])


if __name__ == "__main__":
    unittest.main()
