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
        """普通帧后没有紧随的缺帧占位：新增片段必须取得连续编号，
        其后的数字帧/小写衍生半页帧整体顺延，且全卷不得出现空号或重号。"""
        st = self._state()
        f9 = self._frame(st, "9")
        total_before = len(st["frames"])
        r = self.c.post("/api/frame/%d/boundary/split" % f9["id"],
                        json={"cuts": [{"axis": "x", "pos": 450}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        st2 = r.get_json()["state"]
        self.assertEqual(len(st2["frames"]), total_before + 1)

        nos = [(f["position"], f["frame_no"]) for f in st2["frames"]]
        pos9 = self._frame(st2, "9")["position"]
        # 锚点 No.9 不动；新增片段紧随其后取 No.10
        self.assertEqual(nos[pos9][1], "9")
        new_seg = st2["frames"][pos9 + 1]
        self.assertEqual(new_seg["frame_no"], "10")
        self.assertFalse(new_seg["placeholder"])
        # 原 No.10..37 整体顺延一号（其位置因插入而后移一位）
        self.assertEqual(st2["frames"][pos9 + 2]["frame_no"], "11")
        self.assertEqual(self._frame(st2, "11")["filename"],
                         next(f for f in st["frames"] if f["frame_no"] == "10")["filename"])
        self.assertEqual(self._frame(st2, "38")["filename"],
                         next(f for f in st["frames"] if f["frame_no"] == "37")["filename"])
        # 小写衍生半页随数字核顺延：17b -> 18b（与 18 -> 19 对齐）
        self.assertIsNone(next((f for f in st2["frames"] if f["frame_no"] == "17b"), None))
        b18 = next(f for f in st2["frames"] if f["frame_no"] == "18b")
        self.assertTrue(b18["filename"].endswith("0017b.jpg"))
        # 无空号、无重号
        labels = [f["frame_no"] for f in st2["frames"]]
        self.assertEqual(all(x.strip() for x in labels), True)
        self.assertEqual(len(labels), len(set(labels)))

        # 远处缺帧告警中的帧号同步更新（原缺帧 No.20 -> No.21）
        missing = [w["frame_no"] for w in st2["warnings"] if w["type"] == "missing"]
        self.assertIn("21", missing)

        # 移交清单不得出现未编号帧
        man = self.c.get("/api/reels/%d/export/manifest.json" % self.rid).get_json()
        self.assertTrue(all(row["frame_no"] for row in man["frames"]))

        # 撤销：编号、位置、半页号恢复
        u = self.c.post("/api/reels/%d/undo" % self.rid).get_json()["state"]
        labels = [f["frame_no"] for f in u["frames"]]
        self.assertEqual(len(u["frames"]), total_before)
        self.assertIn("17b", labels)
        self.assertNotIn("18b", labels)
        self.assertEqual(self._frame(u, "9")["filename"], f9["filename"])
        self.assertEqual(len(labels), len(set(labels)))

    def test_split_multiple_new_segments_renumber_correctly(self):
        """一次切成三段（两条切线、无占位）：新增两段取连续号，后续整体 +2 顺延。"""
        st = self._state()
        f11 = self._frame(st, "11")
        r = self.c.post("/api/frame/%d/boundary/split" % f11["id"],
                        json={"cuts": [{"axis": "x", "pos": 300},
                                       {"axis": "x", "pos": 600}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        st2 = r.get_json()["state"]
        self.assertEqual(len(st2["frames"]), len(st["frames"]) + 2)
        window = {f["frame_no"] for f in st2["frames"][10:16]}
        self.assertEqual(window, {"10", "11", "12", "13", "14", "15"})
        self.assertEqual(self._frame(st2, "11")["filename"][:2], "op")
        self.assertEqual(self._frame(st2, "12")["filename"][:2], "op")
        self.assertEqual(self._frame(st2, "13")["filename"][:2], "op")
        # 原 No.12 -> 14
        self.assertTrue(self._frame(st2, "14")["filename"].endswith("0012.jpg"))
        # 17b -> 19b（顺延 2）
        self.assertIsNotNone(next((f for f in st2["frames"] if f["frame_no"] == "19b"), None))
        labels = [f["frame_no"] for f in st2["frames"]]
        self.assertTrue(all(labels))
        self.assertEqual(len(labels), len(set(labels)))

    def test_split_new_segment_then_fill_placeholder_chain(self):
        """先在无占位处拆分（占位 No.20 被顺延到 No.21），再拆分粘连 No.7，
        其第二片段应填充紧随其后的 No.8 占位，编号不发生二次顺延。"""
        # 先拆 No.9（+1 顺延）
        st = self._state()
        f9 = self._frame(st, "9")
        r = self.c.post("/api/frame/%d/boundary/split" % f9["id"],
                        json={"cuts": [{"axis": "x", "pos": 450}]})
        self.assertEqual(r.status_code, 200)
        # 再拆 No.7：第二片段填充 No.8 占位（该占位位置紧随 No.7，不受顺延影响）
        st = r.get_json()["state"]
        f7 = self._frame(st, "7")
        cand = self._candidates()
        cut = next(s for s in cand["splits"] if s["frame_no"] == "7")["cuts"]
        r = self.c.post("/api/frame/%d/boundary/split" % f7["id"], json={"cuts": cut})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        st2 = r.get_json()["state"]
        f8 = self._frame(st2, "8")
        self.assertFalse(f8["placeholder"])
        self.assertEqual(f8["boundary"]["kind"], "crop")
        # 两个拆分操作均在案
        self.assertEqual(len(st2["boundary_ops"]), 2)
        labels = [f["frame_no"] for f in st2["frames"]]
        self.assertTrue(all(labels))
        self.assertEqual(len(labels), len(set(labels)))
        # 逐层撤销恢复
        self.c.post("/api/reels/%d/undo" % self.rid)
        u = self.c.post("/api/reels/%d/undo" % self.rid).get_json()["state"]
        self.assertTrue(self._frame(u, "8")["placeholder"])
        self.assertEqual(self._frame(u, "7")["width"], 1846)
        self.assertEqual(len(u["boundary_ops"]), 0)

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

    def _frame_signature(self):
        """拆分前帧表指纹：id/位置/帧号/文件名/路径/占位/尺寸/旋转。"""
        return [(r["id"], r["position"], r["frame_no"], r["filename"], r["stored_path"],
                 r["placeholder"], r["width"], r["rotation"])
                for r in app.db.frames(self.rid)]

    def test_split_with_preexisting_duplicate_frameno_is_atomic(self):
        """卷内已存在重复 frame_no：拆分必须在任何写入前被预检拒绝，
        不新增帧、不动锚点/位置/编号、不留操作批次/修订/文件，既有撤销栈保持可用。"""
        st = self._state()
        f9 = self._frame(st, "9")
        f11 = self._frame(st, "11")
        # 人工制造卷内重号：No.11 -> "10"（与既有 No.10 重号）
        app.db.run("UPDATE frames SET frame_no='10' WHERE id=?", (f11["id"],))

        sig_before = self._frame_signature()
        rev_before = app.db.one(
            "SELECT COUNT(*) c FROM revisions WHERE reel_id=?", (self.rid,))["c"]
        ops_before = app.db.one("SELECT COUNT(*) c FROM boundary_ops")["c"]
        fver_before = app.db.one("SELECT COUNT(*) c FROM frame_versions")["c"]
        anchor_path = app.db.one("SELECT stored_path FROM frames WHERE id=?", (f9["id"],))["stored_path"]
        bdir = os.path.join(self.tmp, "boundary", str(self.rid))
        os.makedirs(bdir, exist_ok=True)

        r = self.c.post("/api/frame/%d/boundary/split" % f9["id"],
                        json={"cuts": [{"axis": "x", "pos": 450}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("重复帧号", r.get_json()["error"])

        # 帧表逐字段一致：无新增帧、无空号帧、锚点文件/旋转/尺寸未被替换
        self.assertEqual(self._frame_signature(), sig_before)
        self.assertFalse(any(not x[2] for x in self._frame_signature()))
        row = app.db.one("SELECT filename,stored_path,width,rotation FROM frames WHERE id=?",
                         (f9["id"],))
        self.assertEqual(row["stored_path"], anchor_path)
        self.assertEqual(row["width"], 900)
        self.assertEqual(row["rotation"], 0)
        cur = app.db.one(
            "SELECT kind FROM frame_versions WHERE frame_id=? AND is_current=1", (f9["id"],))
        self.assertTrue(cur is None or cur["kind"] != "boundary")
        # 不产生操作批次、修订、边界版本或文件
        self.assertEqual(app.db.one("SELECT COUNT(*) c FROM boundary_ops")["c"], ops_before)
        self.assertEqual(app.db.one(
            "SELECT COUNT(*) c FROM revisions WHERE reel_id=?", (self.rid,))["c"], rev_before)
        self.assertEqual(app.db.one("SELECT COUNT(*) c FROM frame_versions")["c"], fver_before)
        self.assertEqual(os.listdir(bdir), [])

    def test_split_mid_write_failure_hard_restores_and_keeps_undo_stack(self):
        """写入阶段意外失败（第 1 段锚点已替换之后）：按拆分前快照硬恢复，
        锚点/占位/位置/编号/版本指针全部还原，生成文件删除，既有修订与撤销记录不被破坏。"""
        st = self._state()
        f5 = self._frame(st, "5")
        f7 = self._frame(st, "7")
        f8 = self._frame(st, "8")
        # 先造一个合法的既有修订（旋转），失败后必须保留且仍可撤销
        self.c.post("/api/frame/%d/rotate" % f5["id"], json={"deg": 90})
        rev_actions_before = [r["action"] for r in app.db.q(
            "SELECT action FROM revisions WHERE reel_id=? ORDER BY id", (self.rid,))]

        glued_path = app.db.one("SELECT stored_path FROM frames WHERE id=?", (f7["id"],))["stored_path"]
        sig_before = self._frame_signature()
        fver_before = app.db.one("SELECT COUNT(*) c FROM frame_versions")["c"]
        cand = self._candidates()
        cut = next(s for s in cand["splits"] if s["frame_no"] == "7")["cuts"]
        bdir = os.path.join(self.tmp, "boundary", str(self.rid))

        # 让第 2 次 fingerprint（填充占位片段）抛错，此时锚点段已写入、占位尚未补图
        from qc_core import imaging
        orig_fp, n_calls = imaging.fingerprint, {"n": 0}

        def boom(path):
            n_calls["n"] += 1
            if n_calls["n"] == 2:
                raise RuntimeError("simulated write-phase failure")
            return orig_fp(path)

        imaging.fingerprint = boom
        try:
            with self.assertRaises(RuntimeError):
                self.c.post("/api/frame/%d/boundary/split" % f7["id"], json={"cuts": cut})
        finally:
            imaging.fingerprint = orig_fp

        # 帧表逐字段还原（含占位恢复缺帧、锚点恢复粘连图、位置/编号还原）
        self.assertEqual(self._frame_signature(), sig_before)
        nrow = app.db.one("SELECT COUNT(*) c FROM frames WHERE reel_id=?", (self.rid,))["c"]
        self.assertEqual(nrow, len(sig_before))
        a8 = app.db.one("SELECT placeholder,width FROM frames WHERE id=?", (f8["id"],))
        self.assertEqual((a8["placeholder"], a8["width"]), (1, 0))
        a7 = app.db.one("SELECT stored_path,width FROM frames WHERE id=?", (f7["id"],))
        self.assertEqual(a7["stored_path"], glued_path)
        self.assertEqual(a7["width"], 1846)
        cur = app.db.one(
            "SELECT kind FROM frame_versions WHERE frame_id=? AND is_current=1", (f7["id"],))
        self.assertEqual(cur["kind"], "original")
        self.assertEqual(app.db.one(
            "SELECT COUNT(*) c FROM frame_versions WHERE frame_id=? AND kind='boundary'",
            (f8["id"],))["c"], 0)
        self.assertEqual(app.db.one("SELECT COUNT(*) c FROM frame_versions")["c"], fver_before)
        # 操作批次、来源、文件均清理
        self.assertEqual(app.db.one("SELECT COUNT(*) c FROM boundary_ops")["c"], 0)
        self.assertEqual(app.db.one("SELECT COUNT(*) c FROM frame_sources WHERE reel_id=?",
                                   (self.rid,))["c"], 0)
        self.assertEqual(os.listdir(bdir), [])
        # 失败的拆分修订被清掉，只保留既有的旋转修订，且仍可撤销
        rev_actions_after = [r["action"] for r in app.db.q(
            "SELECT action FROM revisions WHERE reel_id=? ORDER BY id", (self.rid,))]
        self.assertEqual(rev_actions_after, rev_actions_before)
        u = self.c.post("/api/reels/%d/undo" % self.rid)
        self.assertEqual(u.status_code, 200)
        self.assertEqual(app.db.one("SELECT rotation FROM frames WHERE id=?",
                                    (f5["id"],))["rotation"], 0)

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

    def test_merge_numeric_pair_rolls_back_later_numbers(self):
        """合并两个连续纯数字帧（占两个号）：锚点保留原号，其后纯数字帧 -1 回退，
        小写衍生半页随核回退；撤销后全部恢复。"""
        st = self._state()
        # 选尾部两个连续实体帧 No.35 / No.36（35 为 .jpg、36 为 .jpg，同卷）
        f35 = self._frame(st, "35")
        f36 = self._frame(st, "36")
        r = self.c.post("/api/reels/%d/boundary/merge" % self.rid,
                        json={"frame_ids": [f35["id"], f36["id"]]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        st2 = r.get_json()["state"]
        self.assertEqual(len(st2["frames"]), len(st["frames"]) - 1)
        # 原片尾 No.37（trailer）回退为 36
        trailer = st2["frames"][-1]
        self.assertEqual(trailer["frame_no"], "36")
        self.assertIn("trailer", trailer["filename"])
        # 锚点仍是 35；编号无空号无重号
        self.assertEqual(self._frame(st2, "35")["boundary"]["kind"], "stitch")
        labels = [f["frame_no"] for f in st2["frames"]]
        self.assertTrue(all(labels))
        self.assertEqual(len(labels), len(set(labels)))

        u = self.c.post("/api/reels/%d/undo" % self.rid).get_json()["state"]
        self.assertEqual(self._frame(u, "36")["filename"], f36["filename"])
        self.assertEqual(u["frames"][-1]["frame_no"], "37")

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
