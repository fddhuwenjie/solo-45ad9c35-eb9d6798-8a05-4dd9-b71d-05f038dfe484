"""微缩胶片扫描卷盘质检工具 —— Flask 主应用。所有分析均在本机完成。"""
import csv
import io
import json
import os
import re
import time
import zipfile

from flask import Flask, abort, jsonify, render_template, request, send_file

from qc_core.db import DB
from qc_core import analysis, boundary, imaging, rescan, review, sample_reel

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
FRAMES_DIR = os.path.join(DATA, "frames")
RESCAN_DIR = os.path.join(DATA, "rescan")
BOUNDARY_DIR = os.path.join(DATA, "boundary")
EXPORT_DIR = os.path.join(DATA, "exports")
os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(RESCAN_DIR, exist_ok=True)
os.makedirs(BOUNDARY_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

app = Flask(__name__)
db = DB(os.path.join(DATA, "qc.db"))

IMG_EXT = {".tif", ".tiff", ".jpg", ".jpeg"}

# 为升级前已存在的帧补建原始版本记录（来源关系）
rescan.seed_original_versions(db)


# ---------------------------------------------------------------- 工具

def reel_or_404(reel_id):
    r = db.one("SELECT * FROM reels WHERE id=?", (reel_id,))
    if not r:
        abort(404, "卷盘不存在")
    return r


def frame_or_404(frame_id):
    f = db.one("SELECT * FROM frames WHERE id=?", (frame_id,))
    if not f:
        abort(404, "帧不存在")
    return f


def renumber(reel_id):
    """按 position 重排为连续序号。"""
    rows = db.frames(reel_id)
    for i, r in enumerate(rows):
        if r["position"] != i:
            db.run("UPDATE frames SET position=? WHERE id=?", (i, r["id"]))


def parse_manifest(text):
    """解析拍摄清单 CSV/JSON -> [{frame_no, filename, note}]。"""
    text = text.strip()
    if not text:
        return []
    if text[0] in "[{":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("frames", [])
        return [{"frame_no": str(d.get("frame_no", d.get("frame", ""))),
                 "filename": d.get("filename", d.get("file", "")),
                 "note": d.get("note", "")} for d in data]
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        lower = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        rows.append({
            "frame_no": lower.get("frame_no") or lower.get("frame") or lower.get("帧号") or "",
            "filename": lower.get("filename") or lower.get("file") or lower.get("文件名") or "",
            "note": lower.get("note") or lower.get("备注") or "",
        })
    return [r for r in rows if r["frame_no"] or r["filename"]]


def import_reel(name, reel_no, zip_bytes, manifest_entries):
    """落盘帧图像并建立帧记录（含清单有而 ZIP 无的缺帧占位）。"""
    cur = db.run("INSERT INTO reels(name, reel_no, created_at) VALUES(?,?,?)",
                 (name, reel_no, time.time()))
    reel_id = cur.lastrowid
    rdir = os.path.join(FRAMES_DIR, str(reel_id))
    os.makedirs(rdir, exist_ok=True)

    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    by_base = {}
    for info in zf.infolist():
        ext = os.path.splitext(info.filename)[1].lower()
        if not info.is_dir() and ext in IMG_EXT:
            by_base[os.path.basename(info.filename)] = info

    used, position = set(), 0

    def add_image_frame(entry, fname_zip):
        nonlocal position
        info = by_base[fname_zip]
        stored = os.path.join(rdir, os.path.basename(info.filename))
        with open(stored, "wb") as fh:
            fh.write(zf.read(info))
        fp = imaging.fingerprint(stored)
        fid = db.add_frame(reel_id, position=position, frame_no=entry["frame_no"],
                           filename=os.path.basename(info.filename), stored_path=stored,
                           note=entry.get("note", ""), **fp)
        db.add_version(fid, reel_id, "original", os.path.basename(info.filename), stored,
                       source="初次导入", is_current=1)
        used.add(fname_zip)
        position += 1

    def add_placeholder(entry):
        nonlocal position
        db.add_frame(reel_id, position=position, frame_no=entry["frame_no"],
                     filename=entry.get("filename", ""), note=entry.get("note", ""),
                     placeholder=1)
        position += 1

    if manifest_entries:
        for e in manifest_entries:
            base = os.path.basename(e.get("filename", "") or "")
            if base and base in by_base:
                add_image_frame(e, base)
            else:
                add_placeholder(e)
        # ZIP 中清单外的文件追加到末尾
        for base in sorted(by_base):
            if base not in used:
                m = re.search(r"\d+", os.path.splitext(base)[0])
                add_image_frame({"frame_no": m.group() if m else base,
                                 "note": "清单外文件"}, base)
    else:
        for base in sorted(by_base):
            m = re.search(r"\d+", os.path.splitext(base)[0])
            add_image_frame({"frame_no": m.group() if m else base}, base)

    renumber(reel_id)
    analysis.run_checks(db, reel_id)
    return reel_id


def state(reel_id):
    reel = reel_or_404(reel_id)
    versions = db.q(
        "SELECT frame_id, kind, source FROM frame_versions WHERE reel_id=? AND is_current=1",
        (reel_id,))
    vsrc = {v["frame_id"]: {"kind": v["kind"], "source": v["source"]} for v in versions}
    n_versions = {r["frame_id"]: r["n"] for r in db.q(
        "SELECT frame_id, COUNT(*) n FROM frame_versions WHERE reel_id=? GROUP BY frame_id",
        (reel_id,))}
    source_rows = db.q(
        """SELECT fs.* FROM frame_sources fs
           WHERE fs.id IN (SELECT MAX(id) FROM frame_sources WHERE reel_id=? GROUP BY frame_id)""",
        (reel_id,))
    src_map = {}
    for s in source_rows:
        region = {}
        try:
            region = json.loads(s["region"]) if s["region"] else {}
        except ValueError:
            pass
        src_map[s["frame_id"]] = {
            "kind": s["kind"], "op_id": s["op_id"],
            "source_frame_id": s["source_frame_id"],
            "source_filename": s["source_filename"],
            "region_axis": region.get("axis"),
            "region_index": region.get("index"),
            "region_count": region.get("count"),
            "region_layout": region.get("layout"),
        }
    frames = []
    for f in db.frames(reel_id):
        d = {k: f[k] for k in ("id", "position", "frame_no", "filename", "note",
                               "brightness", "orient_score", "rotation",
                               "excluded", "placeholder", "reshoot", "width", "height")}
        d["source"] = vsrc.get(f["id"], {}).get("source", "")
        d["version_kind"] = vsrc.get(f["id"], {}).get("kind", "")
        d["version_count"] = n_versions.get(f["id"], 0)
        d["boundary"] = src_map.get(f["id"])
        frames.append(d)
    warnings = [dict(w) for w in db.q(
        "SELECT * FROM warnings WHERE reel_id=? ORDER BY resolved, id", (reel_id,))]
    return {
        "reel": {"id": reel["id"], "name": reel["name"], "reel_no": reel["reel_no"],
                 "finalized": reel["finalized"]},
        "frames": frames,
        "warnings": warnings,
        "can_undo": len(db.revisions(reel_id)) > 0,
        "rescan_batches": rescan.list_batches(db, reel_id),
        "boundary_ops": boundary.ops_list(db, reel_id),
        "checks": finalization_checks_with_review(reel_id),
        "review": review.review_summary(db, reel_id),
    }


def finalization_checks_with_review(reel_id):
    """定稿检查 + 二次验收通过结论门禁（state 与 finalize 共用）。"""
    checks = analysis.finalization_checks(db, reel_id)
    vp = review.valid_pass(db, reel_id)
    review_check = {"key": "review", "ok": vp is not None, "label": "二次验收",
                    "detail": ("已关联第 %d 轮通过结论（种子 %s，复核人 %s）"
                               % (vp["seq"], vp["seed"], vp["reviewer"])) if vp
                    else "缺少有效的二次验收通过结论，请完成抽查复核"}
    checks["checks"].append(review_check)
    checks["passed"] = checks["passed"] and review_check["ok"]
    return checks


def mutate(reel_id, action, fn):
    """保存修订快照 -> 执行变更 -> 重算连续性。"""
    db.save_revision(reel_id, action)
    fn()
    renumber(reel_id)
    db.run("UPDATE reels SET finalized=0 WHERE id=?", (reel_id,))
    analysis.run_checks(db, reel_id)
    return jsonify(state(reel_id))


def rollback_rescan(extra):
    """撤销时回滚补扫条目/版本状态（帧字段已由快照还原）。"""
    for op in extra.get("rescan", []):
        item = db.one("SELECT * FROM rescan_items WHERE id=?", (op["item_id"],))
        if not item:
            continue
        if op["op"] == "accept":
            # 仅回滚接受状态；配准核对记录（reg_status/reg_detail/reg_manual/force_reason）
            # 必须保留——撤销接受是为了重新决定，不应丢失已完成的图像核对证据。
            db.run("UPDATE rescan_items SET status='pending', decided_at=0 WHERE id=?",
                   (op["item_id"],))
            # 新版本失效，原版本（若有）恢复当前
            db.run("""UPDATE frame_versions SET is_current=0
                      WHERE frame_id=? AND item_id=?""",
                   (op["frame_id"], op["item_id"]))
            if op.get("old_version_id"):
                db.run("UPDATE frame_versions SET is_current=1 WHERE id=?",
                       (op["old_version_id"],))
        elif op["op"] == "decide":
            old_target = op.get("old_target_frame_id")
            db.run("UPDATE rescan_items SET status=?, block_reason=?, decision_note=?, decided_at=0 WHERE id=?",
                   (op["status"], op.get("block_reason", ""),
                    op.get("decision_note", ""), op["item_id"]))
            if old_target is not None:
                db.run("UPDATE rescan_items SET target_frame_id=? WHERE id=?",
                       (old_target, op["item_id"]))
            if op.get("old_frame_no"):
                db.run("UPDATE rescan_items SET frame_no=? WHERE id=?",
                       (op["old_frame_no"], op["item_id"]))


# ---------------------------------------------------------------- 页面与卷盘

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/reels")
def reels():
    rows = db.q("""SELECT r.*, (SELECT COUNT(*) FROM frames f WHERE f.reel_id=r.id) n
                   FROM reels r ORDER BY r.id DESC""")
    return jsonify([{"id": r["id"], "name": r["name"], "reel_no": r["reel_no"],
                     "frames": r["n"], "finalized": r["finalized"]} for r in rows])


@app.route("/api/reels/<int:reel_id>/state")
def reel_state(reel_id):
    return jsonify(state(reel_id))


@app.route("/api/sample", methods=["POST"])
def load_sample():
    zip_bytes, manifest_bytes = sample_reel.build_sample()
    entries = parse_manifest(manifest_bytes.decode("utf-8-sig"))
    reel_id = import_reel("演示卷盘（内置样例）", sample_reel.REEL_NO, zip_bytes, entries)
    return jsonify({"reel_id": reel_id, "state": state(reel_id)})


@app.route("/api/import", methods=["POST"])
def import_zip():
    zf = request.files.get("zip")
    if not zf:
        abort(400, "请上传帧图像 ZIP")
    manifest = request.files.get("manifest")
    entries = []
    if manifest:
        entries = parse_manifest(manifest.read().decode("utf-8-sig", "ignore"))
    name = request.form.get("name") or os.path.splitext(zf.filename)[0]
    reel_no = request.form.get("reel_no") or name
    reel_id = import_reel(name, reel_no, zf.read(), entries)
    return jsonify({"reel_id": reel_id, "state": state(reel_id)})


@app.route("/api/reels/<int:reel_id>", methods=["DELETE"])
def delete_reel(reel_id):
    reel_or_404(reel_id)
    db.run("DELETE FROM warnings WHERE reel_id=?", (reel_id,))
    db.run("DELETE FROM revisions WHERE reel_id=?", (reel_id,))
    db.run("DELETE FROM frame_versions WHERE reel_id=?", (reel_id,))
    db.run("DELETE FROM frame_sources WHERE reel_id=?", (reel_id,))
    db.run("DELETE FROM boundary_ops WHERE reel_id=?", (reel_id,))
    db.run("DELETE FROM frames WHERE reel_id=?", (reel_id,))
    db.run("DELETE FROM rescan_batches WHERE reel_id=?", (reel_id,))
    db.run("DELETE FROM review_judgements WHERE round_id IN "
           "(SELECT id FROM review_rounds WHERE reel_id=?)", (reel_id,))
    db.run("DELETE FROM review_items WHERE reel_id=?", (reel_id,))
    db.run("DELETE FROM review_rounds WHERE reel_id=?", (reel_id,))
    db.run("DELETE FROM reels WHERE id=?", (reel_id,))
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 帧图像

@app.route("/api/frame/<int:frame_id>/thumb")
def thumb(frame_id):
    f = frame_or_404(frame_id)
    w = max(40, min(1200, int(request.args.get("w", 160))))
    if f["placeholder"]:
        data = imaging.placeholder_thumb(f["frame_no"], w)
    else:
        data = imaging.make_thumb(f["stored_path"], f["rotation"], w)
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.route("/api/frame/<int:frame_id>/preview")
def preview(frame_id):
    f = frame_or_404(frame_id)
    w = max(200, min(2400, int(request.args.get("w", 900))))
    if f["placeholder"]:
        data = imaging.placeholder_thumb(f["frame_no"], w)
    else:
        data = imaging.make_thumb(f["stored_path"], f["rotation"], w)
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


# ---------------------------------------------------------------- 编辑操作

@app.route("/api/reels/<int:reel_id>/move", methods=["POST"])
def move(reel_id):
    reel_or_404(reel_id)
    body = request.get_json(force=True)
    fid, to = int(body["frame_id"]), int(body["to_index"])

    def op():
        rows = db.frames(reel_id)
        ids = [r["id"] for r in rows]
        ids.remove(fid)
        ids.insert(max(0, min(to, len(ids))), fid)
        for i, iid in enumerate(ids):
            db.run("UPDATE frames SET position=? WHERE id=?", (i, iid))

    return mutate(reel_id, "拖拽调整帧序", op)


@app.route("/api/frame/<int:frame_id>/rotate", methods=["POST"])
def rotate(frame_id):
    f = frame_or_404(frame_id)
    deg = int(request.get_json(force=True).get("deg", 90))

    def op():
        newrot = (f["rotation"] + deg) % 360
        db.run("UPDATE frames SET rotation=? WHERE id=?", (newrot, frame_id))
        if not f["placeholder"]:
            fp = imaging.fingerprint(f["stored_path"], newrot)
            db.run("UPDATE frames SET phash=?, brightness=?, orient_score=? WHERE id=?",
                   (fp["phash"], fp["brightness"], fp["orient_score"], frame_id))

    return mutate(f["reel_id"], "旋转帧 No.%s %d°" % (f["frame_no"], deg), op)


@app.route("/api/frame/<int:frame_id>/exclude", methods=["POST"])
def exclude(frame_id):
    f = frame_or_404(frame_id)
    val = 1 if request.get_json(force=True).get("value", True) else 0

    def op():
        db.run("UPDATE frames SET excluded=? WHERE id=?", (val, frame_id))

    act = "剔除帧 No.%s" % f["frame_no"] if val else "恢复帧 No.%s" % f["frame_no"]
    return mutate(f["reel_id"], act, op)


@app.route("/api/frame/<int:frame_id>/reshoot", methods=["POST"])
def reshoot(frame_id):
    f = frame_or_404(frame_id)
    val = 1 if request.get_json(force=True).get("value", True) else 0

    def op():
        db.run("UPDATE frames SET reshoot=? WHERE id=?", (val, frame_id))

    act = "标记重拍 No.%s" % f["frame_no"] if val else "取消重拍 No.%s" % f["frame_no"]
    return mutate(f["reel_id"], act, op)


@app.route("/api/reels/<int:reel_id>/placeholder", methods=["POST"])
def insert_placeholder(reel_id):
    reel_or_404(reel_id)
    body = request.get_json(force=True)
    frame_no = str(body.get("frame_no", "")).strip()
    if not frame_no:
        abort(400, "请提供占位帧号")
    index = int(body.get("index", 10 ** 9))

    def op():
        rows = db.frames(reel_id)
        index2 = max(0, min(index, len(rows)))
        for i, r in enumerate(rows):
            if i >= index2:
                db.run("UPDATE frames SET position=? WHERE id=?", (i + 1, r["id"]))
        db.add_frame(reel_id, position=index2, frame_no=frame_no, placeholder=1,
                     note="人工插入缺帧占位")

    return mutate(reel_id, "插入缺帧占位 No.%s" % frame_no, op)


@app.route("/api/frame/<int:frame_id>", methods=["DELETE"])
def delete_frame(frame_id):
    f = frame_or_404(frame_id)
    if not f["placeholder"]:
        abort(400, "仅可删除占位帧（实体帧请用剔除）")

    def op():
        db.run("DELETE FROM frames WHERE id=?", (frame_id,))

    return mutate(f["reel_id"], "删除占位帧 No.%s" % f["frame_no"], op)


# ---------------------------------------------------------------- 帧边界复核

def _boundary_abort_revision(reel_id):
    db.run("DELETE FROM revisions WHERE id=(SELECT MAX(id) FROM revisions WHERE reel_id=?)",
           (reel_id,))


@app.route("/api/reels/<int:reel_id>/boundary/candidates")
def boundary_candidates(reel_id):
    reel_or_404(reel_id)
    return jsonify(boundary.candidates(db, reel_id))


@app.route("/api/frame/<int:frame_id>/boundary/preview")
def boundary_split_preview(frame_id):
    f = frame_or_404(frame_id)
    if f["placeholder"]:
        abort(400, "占位帧没有图像")
    cuts = []
    for item in request.args.getlist("cut"):
        m = re.match(r"^([xy]):(-?\d+)$", item)
        if not m:
            abort(400, "切线格式应为 x:1234 或 y:567")
        cuts.append({"axis": m.group(1), "pos": int(m.group(2))})
    index = request.args.get("index", "")
    index = int(index) if index.lstrip("-").isdigit() else None
    w = max(200, min(2400, int(request.args.get("w", 1100))))
    try:
        data = boundary.split_preview(dict(f), cuts, index=index, out_w=w)
    except ValueError as ex:
        abort(400, str(ex))
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.route("/api/reels/<int:reel_id>/boundary/merge-preview")
def boundary_merge_preview(reel_id):
    reel_or_404(reel_id)
    ids = request.args.get("ids", "")
    try:
        frame_ids = [int(x) for x in ids.split(",") if x]
        frames = [dict(x) for x in db.frames(reel_id)]
        data = boundary.merge_preview(frames, frame_ids,
                                      out_w=max(200, min(2400, int(request.args.get("w", 1100)))))
    except ValueError as ex:
        abort(400, str(ex))
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


def _boundary_recheck(reel_id, outputs, extra_boundary):
    """局部重算参与帧及邻近帧告警；若编号顺延波及窗口之外，则全量重算
    （远处缺帧/重复告警中的旧帧号需要随顺延更新）。"""
    local = set(_neighbors(db, reel_id, outputs))
    far = [fid for fid in (extra_boundary.get("renumber_ids") or []) if fid not in local]
    if far:
        analysis.run_checks(db, reel_id)
    else:
        analysis.recheck(db, reel_id, outputs + _neighbors(db, reel_id, outputs))


@app.route("/api/frame/<int:frame_id>/boundary/split", methods=["POST"])
def boundary_split(frame_id):
    f = frame_or_404(frame_id)
    reel_id = f["reel_id"]
    body = request.get_json(force=True)
    cuts = body.get("cuts") or []
    norm = []
    for c in cuts:
        if (isinstance(c, dict) and c.get("axis") in ("x", "y")
                and str(c.get("pos", "")).lstrip("-").isdigit()):
            norm.append({"axis": c["axis"], "pos": int(c["pos"])})
    if len(norm) != len(cuts):
        abort(400, "切线格式无效（需要 {axis:'x'/'y', pos:像素}）")
    reason = str(body.get("reason", "")).strip()
    # 写入前只读预检：裁切几何、零宽/交叉/越界、拆分后空号/重号等在此全部完成。
    # 预检失败直接返回，绝不创建操作批次、修订或生成任何文件，保证失败零副作用。
    try:
        boundary.plan_split(db, reel_id, frame_id, norm)
    except ValueError as ex:
        return jsonify({"error": str(ex), "state": state(reel_id)}), 400
    op_id = boundary.create_op(db, reel_id, "split", reason)
    db.save_revision(reel_id, "帧边界拆分 No.%s（%d 段）" % (f["frame_no"], len(norm) + 1))
    rev_id = db.one("SELECT MAX(id) id FROM revisions WHERE reel_id=?", (reel_id,))["id"]
    try:
        anchor, outputs, extra, op_id = boundary.split_frame(
            db, DATA, reel_id, frame_id, norm, reason=reason, op_id=op_id)
    except ValueError as ex:
        # split_frame 已按拆分前快照硬恢复帧/文件/版本；这里只清掉本次修订行
        _boundary_abort_revision(reel_id)
        return jsonify({"error": str(ex), "state": state(reel_id)}), 400
    except Exception:
        _boundary_abort_revision(reel_id)
        raise
    db.run("UPDATE reels SET finalized=0 WHERE id=?", (reel_id,))
    _boundary_recheck(reel_id, outputs, extra["boundary"])
    # 二次验收：拆分就地更换了锚点/填充帧图像；新增片段此前不可能被抽中
    affected = [frame_id] + extra["boundary"].get("filled_placeholders", [])
    _void_review_for_extra(reel_id, affected,
                           "帧边界拆分换图（操作 #%s）" % op_id, rev_id, extra)
    return jsonify({"op_id": op_id, "outputs": outputs, "state": state(reel_id)})


def _void_review_for_extra(reel_id, affected_frame_ids, reason, rev_id, extra):
    """作废开放轮次中受换图影响的抽中项，并把回滚信息并入修订 extra。"""
    void_ids = review.invalidate_rounds_for_frames(db, reel_id, affected_frame_ids, reason)
    if void_ids:
        extra["review"] = [{"op": "void", "item_id": iid}
                           for iid in dict.fromkeys(void_ids)]
    db.run("UPDATE revisions SET extra=? WHERE id=?",
           (json.dumps(extra, ensure_ascii=False), rev_id))


@app.route("/api/reels/<int:reel_id>/boundary/merge", methods=["POST"])
def boundary_merge(reel_id):
    reel_or_404(reel_id)
    body = request.get_json(force=True)
    frame_ids = [int(x) for x in (body.get("frame_ids") or [])]
    if not frame_ids:
        abort(400, "请先选择要合并的连续帧")
    layout = body.get("layout") or None
    reason = str(body.get("reason", "")).strip()
    frames = [dict(x) for x in db.frames(reel_id)]
    try:
        ordered = boundary._ordered_merge(frames, frame_ids)  # 先做连续性校验
    except ValueError as ex:
        return jsonify({"error": str(ex), "state": state(reel_id)}), 400
    label = "、".join("No." + x["frame_no"] for x in ordered)
    op_id = boundary.create_op(db, reel_id, "merge", reason)
    db.save_revision(reel_id, "帧边界合并 %s" % label)
    rev_id = db.one("SELECT MAX(id) id FROM revisions WHERE reel_id=?", (reel_id,))["id"]
    try:
        anchor, outputs, extra, op_id = boundary.merge_frames(
            db, DATA, reel_id, frame_ids, layout=layout, reason=reason, op_id=op_id)
    except ValueError as ex:
        boundary.discard_op(db, op_id)
        _boundary_abort_revision(reel_id)
        return jsonify({"error": str(ex), "state": state(reel_id)}), 400
    db.run("UPDATE reels SET finalized=0 WHERE id=?", (reel_id,))
    _boundary_recheck(reel_id, outputs, extra["boundary"])
    # 二次验收：合并后锚点帧图像被拼接结果替换；被删帧在其悬空期也按受影响处理
    affected = frame_ids
    _void_review_for_extra(reel_id, affected,
                           "帧边界合并换图（操作 #%s）" % op_id, rev_id, extra)
    return jsonify({"op_id": op_id, "outputs": outputs, "state": state(reel_id)})


def _neighbors(db, reel_id, ids):
    """局部重算窗口：参与帧在当前序列中的邻近帧。"""
    frames = db.frames(reel_id)
    id_set, out = set(ids), []
    for i, f in enumerate(frames):
        if f["id"] in id_set:
            for k in range(max(0, i - analysis.RECHECK_RADIUS),
                           min(len(frames), i + analysis.RECHECK_RADIUS + 1)):
                out.append(frames[k]["id"])
    return out


@app.route("/api/reels/<int:reel_id>/boundary/ops")
def boundary_ops(reel_id):
    reel_or_404(reel_id)
    return jsonify(boundary.ops_list(db, reel_id))


@app.route("/api/reels/<int:reel_id>/boundary/export/changes.json")
def boundary_export_changes(reel_id):
    reel = reel_or_404(reel_id)
    payload = boundary.changes_json(db, reel_id, reel["reel_no"], reel["name"])
    buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    return send_file(buf, mimetype="application/json", as_attachment=True,
                     download_name="%s_boundary_changes.json" % reel["reel_no"])


@app.route("/api/boundary-ops/<int:op_id>/comparison.png")
def boundary_export_comparison(op_id):
    op = db.one("SELECT * FROM boundary_ops WHERE id=?", (op_id,))
    if not op:
        abort(404, "边界修订不存在")
    out = os.path.join(EXPORT_DIR, "boundary_compare_%d.png" % op_id)
    data = boundary.comparison_png(db, op_id, path=out)
    if data is None:
        abort(404, "无法生成对照图（原图可能已清理）")
    return send_file(out, mimetype="image/png", as_attachment=True,
                     download_name="boundary_compare_op%d.png" % op_id)


# ---------------------------------------------------------------- 告警 / 撤销 / 定稿

@app.route("/api/warning/<int:warning_id>/resolve", methods=["POST"])
def resolve_warning(warning_id):
    w = db.one("SELECT * FROM warnings WHERE id=?", (warning_id,))
    if not w:
        abort(404)
    val = 1 if request.get_json(force=True).get("value", True) else 0
    db.run("UPDATE warnings SET resolved=? WHERE id=?", (val, warning_id))
    return jsonify(state(w["reel_id"]))


@app.route("/api/reels/<int:reel_id>/undo", methods=["POST"])
def undo(reel_id):
    reel_or_404(reel_id)
    result = db.undo(reel_id)
    if result is None:
        return jsonify({"error": "没有可撤销的操作", "state": state(reel_id)}), 400
    if result.get("extra"):
        rollback_rescan(result["extra"])
        boundary.rollback(db, result["extra"])
        review.undo_invalidate(
            db, [op.get("item_id") for op in result["extra"].get("review", [])
                 if op.get("op") == "void"])
    db.run("UPDATE reels SET finalized=0 WHERE id=?", (reel_id,))
    analysis.run_checks(db, reel_id)
    return jsonify({"undone": result["action"], "state": state(reel_id)})


@app.route("/api/reels/<int:reel_id>/finalize", methods=["POST"])
def finalize(reel_id):
    reel_or_404(reel_id)
    checks = finalization_checks_with_review(reel_id)
    if not checks["passed"]:
        return jsonify({"error": "定稿检查未通过", "checks": checks, "state": state(reel_id)}), 400
    db.run("UPDATE reels SET finalized=1 WHERE id=?", (reel_id,))
    return jsonify(state(reel_id))


# ---------------------------------------------------------------- 二次验收（抽查复核）

@app.route("/api/reels/<int:reel_id>/reviews/settings", methods=["POST"])
def review_set_default_limit(reel_id):
    reel_or_404(reel_id)
    body = request.get_json(force=True, silent=True) or {}
    try:
        limit = int(body.get("fail_limit", 0))
    except (TypeError, ValueError):
        abort(400, "默认失败门限必须是非负整数")
    if limit < 0:
        abort(400, "默认失败门限不能为负")
    db.set_setting(review.SETTING_FAIL_LIMIT, limit)
    return jsonify(state(reel_id))


@app.route("/api/reels/<int:reel_id>/reviews/start", methods=["POST"])
def review_start(reel_id):
    reel_or_404(reel_id)
    body = request.get_json(force=True, silent=True) or {}
    try:
        params = review.validate_params(db, body)
        round_id = review.create_round(db, reel_id, params)
    except ValueError as ex:
        return jsonify({"error": str(ex), "state": state(reel_id)}), 400
    db.run("UPDATE reels SET finalized=0 WHERE id=?", (reel_id,))
    return jsonify({"round_id": round_id,
                    "detail": review.round_detail(db, round_id),
                    "state": state(reel_id)})


@app.route("/api/reviews/<int:round_id>")
def review_detail(round_id):
    r = db.one("SELECT * FROM review_rounds WHERE id=?", (round_id,))
    if not r:
        abort(404, "抽查轮次不存在")
    include_audit = bool(request.args.get("audit"))
    return jsonify(review.round_detail(db, round_id, include_audit=include_audit))


@app.route("/api/review-items/<int:item_id>/image")
def review_item_image(item_id):
    """匿名审片图像：始终返回抽样时锁定的版本与旋转，路径/帧号不出现在页面上。"""
    it = db.one("SELECT * FROM review_items WHERE id=?", (item_id,))
    if not it:
        abort(404, "抽查项不存在")
    path = it["locked_path"]
    if not path or not os.path.exists(path):
        abort(404, "锁定图像文件已不可读")
    w = max(200, min(2400, int(request.args.get("w", 1000))))
    data = imaging.make_thumb(path, it["locked_rotation"], w)
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.route("/api/review-items/<int:item_id>/judge", methods=["POST"])
def review_judge(item_id):
    it = db.one("SELECT * FROM review_items WHERE id=?", (item_id,))
    if not it:
        abort(404, "抽查项不存在")
    body = request.get_json(force=True, silent=True) or {}
    reviewer = str(body.get("reviewer", "") or "")
    try:
        review.submit_judgement(
            db, item_id, reviewer, str(body.get("verdict", "")),
            body.get("dims") or {}, str(body.get("note", "") or ""),
            bool(body.get("transfer_reshoot")))
    except ValueError as ex:
        return jsonify({"error": str(ex)}), 400
    return jsonify({"detail": review.round_detail(db, it["round_id"])})


@app.route("/api/reviews/<int:round_id>/finish", methods=["POST"])
def review_finish(round_id):
    r = db.one("SELECT * FROM review_rounds WHERE id=?", (round_id,))
    if not r:
        abort(404, "抽查轮次不存在")
    body = request.get_json(force=True, silent=True) or {}
    action = str(body.get("action", "") or "")
    try:
        status, progress = review.finish_round(
            db, round_id, str(body.get("reviewer", "") or ""), action,
            str(body.get("conclusion", "") or ""))
    except ValueError as ex:
        return jsonify({"error": str(ex),
                        "detail": review.round_detail(db, round_id)}), 400
    db.run("UPDATE reels SET finalized=0 WHERE id=?", (r["reel_id"],))
    return jsonify({"status": status, "progress": progress,
                    "detail": review.round_detail(db, round_id),
                    "state": state(r["reel_id"])})


@app.route("/api/reviews/<int:round_id>/extend", methods=["POST"])
def review_extend(round_id):
    r = db.one("SELECT * FROM review_rounds WHERE id=?", (round_id,))
    if not r:
        abort(404, "抽查轮次不存在")
    body = request.get_json(force=True, silent=True) or {}
    try:
        # 沿用本轮复核人；新数量/占比/种子/门限由表单给（可复现）
        params = review.validate_params(db, dict(body, reviewer=r["reviewer"]))
        new_id = review.extend_round(db, round_id, r["reviewer"], params)
    except ValueError as ex:
        return jsonify({"error": str(ex),
                        "detail": review.round_detail(db, round_id),
                        "state": state(r["reel_id"])}), 400
    db.run("UPDATE reels SET finalized=0 WHERE id=?", (r["reel_id"],))
    return jsonify({"round_id": new_id,
                    "detail": review.round_detail(db, new_id),
                    "state": state(r["reel_id"])})


@app.route("/api/reviews/<int:round_id>/audit")
def review_audit(round_id):
    """非匿名审计视图：暴露帧号/文件/强制标签/审阅历史（管理与移交证据用）。"""
    r = db.one("SELECT * FROM review_rounds WHERE id=?", (round_id,))
    if not r:
        abort(404, "抽查轮次不存在")
    return jsonify(review.round_detail(db, round_id, include_audit=True))


@app.route("/api/reels/<int:reel_id>/reviews/handoff.<fmt>")
def review_handoff(reel_id, fmt):
    """定稿移交：JSON/CSV 写入方案参数、种子、复核人、作废缘由和最终处置。"""
    reel = reel_or_404(reel_id)
    vp = review.valid_pass(db, reel_id)
    if not vp:
        abort(400, "没有有效的二次验收通过结论，不能生成移交文件")
    if fmt == "json":
        payload = review.handoff_payload(db, reel)
        buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        return send_file(buf, mimetype="application/json", as_attachment=True,
                         download_name="%s_review_handoff.json" % reel["reel_no"])
    if fmt == "csv":
        data = review.handoff_csv(db, reel)
        return send_file(io.BytesIO(data), mimetype="text/csv", as_attachment=True,
                         download_name="%s_review_handoff.csv" % reel["reel_no"])
    abort(404, "仅支持 .json / .csv")


# ---------------------------------------------------------------- 补扫回填

def _rescan_batch_or_404(batch_id, reel_id=None):
    b = db.one("SELECT * FROM rescan_batches WHERE id=?", (batch_id,))
    if not b or (reel_id is not None and b["reel_id"] != reel_id):
        abort(404, "补扫批次不存在")
    return b


@app.route("/api/reels/<int:reel_id>/rescans")
def rescan_batches(reel_id):
    reel_or_404(reel_id)
    return jsonify(rescan.list_batches(db, reel_id))


@app.route("/api/reels/<int:reel_id>/rescans/import", methods=["POST"])
def rescan_import(reel_id):
    reel = reel_or_404(reel_id)
    zf = request.files.get("zip")
    if not zf:
        abort(400, "请上传补扫 ZIP")
    manifest = request.files.get("manifest")
    if not manifest:
        abort(400, "请上传包含卷号、原帧号、文件名的回填清单 CSV/JSON")
    entries = rescan.parse_backfill_manifest(manifest.read().decode("utf-8-sig", "ignore"))
    if not entries:
        abort(400, "回填清单为空或无法解析（需要 reel_no, frame_no, filename 列）")
    name = request.form.get("name") or os.path.splitext(zf.filename)[0] or ("补扫批次-%s" % reel["reel_no"])
    note = request.form.get("note", "")
    batch_id = rescan.import_batch(db, DATA, reel_id, reel["reel_no"], name,
                                   zf.read(), entries, note=note)
    detail = rescan.batch_detail(db, batch_id)
    return jsonify({"batch_id": batch_id, "detail": detail, "state": state(reel_id)})


@app.route("/api/rescans/<int:batch_id>")
def rescan_detail(batch_id):
    _rescan_batch_or_404(batch_id)
    detail = rescan.batch_detail(db, batch_id)
    if detail is None:
        abort(404)
    return jsonify(detail)


@app.route("/api/rescan-items/<int:item_id>/accept", methods=["POST"])
def rescan_accept(item_id):
    it = db.one("SELECT * FROM rescan_items WHERE id=?", (item_id,))
    if not it:
        abort(404, "条目不存在")
    b = _rescan_batch_or_404(it["batch_id"])
    body = request.get_json(force=True, silent=True) or {}
    note = body.get("note", "")
    force_reason = str(body.get("force_reason", "") or "").strip()
    db.save_revision(b["reel_id"], "接受补扫回填 No.%s" % it["frame_no"])
    try:
        reel_id, frame_id, extra = rescan.accept_item(
            db, item_id, note=note, force_reason=force_reason)
    except ValueError as ex:
        db.run("DELETE FROM revisions WHERE id=(SELECT MAX(id) FROM revisions WHERE reel_id=?)",
               (b["reel_id"],))
        return jsonify({"error": str(ex),
                        "detail": rescan.batch_detail(db, it["batch_id"])}), 400
    db.run("UPDATE reels SET finalized=0 WHERE id=?", (reel_id,))
    analysis.recheck(db, reel_id, [frame_id])
    # 二次验收：补扫回填更换了当前有效图，开放轮次中该帧的抽中记录作废（历史保留）
    void_ids = review.invalidate_frame(
        db, reel_id, frame_id, "补扫回填换图（补扫批次 #%s）" % item_id)
    if void_ids:
        extra.setdefault("review", []).extend(
            {"op": "void", "item_id": iid} for iid in void_ids)
    # 把补扫回滚信息并入刚保存的修订
    db.run("UPDATE revisions SET extra=? WHERE id=(SELECT MAX(id) FROM revisions WHERE reel_id=?)",
           (json.dumps(extra, ensure_ascii=False), reel_id))
    return jsonify({"detail": rescan.batch_detail(db, it["batch_id"]),
                    "state": state(reel_id)})


@app.route("/api/rescan-items/<int:item_id>/reject", methods=["POST"])
def rescan_reject(item_id):
    it = db.one("SELECT * FROM rescan_items WHERE id=?", (item_id,))
    if not it:
        abort(404, "条目不存在")
    b = _rescan_batch_or_404(it["batch_id"])
    body = request.get_json(force=True, silent=True) or {}
    note = body.get("note", "")
    db.save_revision(b["reel_id"], "拒绝补扫条目 No.%s" % it["frame_no"])
    reel_id, _fid, extra = rescan.reject_item(db, item_id, note=note)
    db.run("UPDATE revisions SET extra=? WHERE id=(SELECT MAX(id) FROM revisions WHERE reel_id=?)",
           (json.dumps(extra, ensure_ascii=False), reel_id))
    return jsonify({"detail": rescan.batch_detail(db, it["batch_id"]),
                    "state": state(reel_id)})


@app.route("/api/rescan-items/<int:item_id>/rebind", methods=["POST"])
def rescan_rebind(item_id):
    it = db.one("SELECT * FROM rescan_items WHERE id=?", (item_id,))
    if not it:
        abort(404, "条目不存在")
    b = _rescan_batch_or_404(it["batch_id"])
    body = request.get_json(force=True)
    new_no = str(body.get("frame_no", "")).strip()
    if not new_no:
        abort(400, "请提供改绑目标帧号")
    note = body.get("note", "改绑 No.%s → No.%s" % (it["frame_no"], new_no))
    db.save_revision(b["reel_id"], "补扫改绑 No.%s → No.%s" % (it["frame_no"], new_no))
    try:
        reel_id, _fid, extra = rescan.rebind_item(db, item_id, new_no, note=note)
    except ValueError as ex:
        db.run("DELETE FROM revisions WHERE id=(SELECT MAX(id) FROM revisions WHERE reel_id=?)",
               (b["reel_id"],))
        return jsonify({"error": str(ex),
                        "detail": rescan.batch_detail(db, it["batch_id"])}), 400
    db.run("UPDATE revisions SET extra=? WHERE id=(SELECT MAX(id) FROM revisions WHERE reel_id=?)",
           (json.dumps(extra, ensure_ascii=False), reel_id))
    return jsonify({"detail": rescan.batch_detail(db, it["batch_id"]),
                    "state": state(reel_id)})


@app.route("/api/rescans/<int:batch_id>/accept-clean", methods=["POST"])
def rescan_accept_clean(batch_id):
    b = _rescan_batch_or_404(batch_id)
    reel_id = b["reel_id"]
    db.save_revision(reel_id, "批量接受补扫批次「%s」配准通过项" % b["name"])
    _rid, changed, extra, report = rescan.accept_clean(db, batch_id)
    errors, skipped = report["errors"], report["skipped"]
    db.run("UPDATE reels SET finalized=0 WHERE id=?", (reel_id,))
    if changed:
        analysis.recheck(db, reel_id, changed)
    db.run("UPDATE revisions SET extra=? WHERE id=(SELECT MAX(id) FROM revisions WHERE reel_id=?)",
           (json.dumps(extra, ensure_ascii=False), reel_id))
    return jsonify({"detail": rescan.batch_detail(db, batch_id),
                    "errors": errors, "skipped": skipped, "state": state(reel_id)})


@app.route("/api/rescan-items/<int:item_id>/registration", methods=["POST"])
def rescan_recompute_registration(item_id):
    """人工微调对齐：按给定旋转/平移重算配准指标并落库（核对记录随之更新）。"""
    it = db.one("SELECT * FROM rescan_items WHERE id=?", (item_id,))
    if not it:
        abort(404, "条目不存在")
    _rescan_batch_or_404(it["batch_id"])
    body = request.get_json(force=True, silent=True) or {}
    manual = None
    # 已接受/已拒绝条目的核对记录作为审计证据冻结，只允许查看（改绑/拒绝时另行重算）
    if it["status"] in ("accepted", "rejected"):
        abort(400, "该条目已处理，核对记录已冻结；如需重新配准请先撤销或改绑")
    if body.get("manual"):
        # 公开契约：manual=true 时读取 rotation/dx/dy（dx/dy 为原图像素平移）。
        # 兼容旧前端曾用的 dx_full/dy_full；两者都没有时视为 0。
        def _field(name, legacy):
            if body.get(name) is not None:
                return float(body[name])
            if body.get(legacy) is not None:
                return float(body[legacy])
            return 0
        try:
            rotation = int(body.get("rotation", 0))
            dx = int(round(_field("dx", "dx_full")))
            dy = int(round(_field("dy", "dy_full")))
        except (TypeError, ValueError):
            abort(400, "旋转/平移参数无效")
        manual = (rotation, dx, dy)
    try:
        _status, detail = rescan.compute_registration(db, item_id, manual=manual)
    except ValueError as ex:
        return jsonify({"error": str(ex)}), 400
    return jsonify({"detail": rescan.batch_detail(db, it["batch_id"]),
                    "registration": detail})


@app.route("/api/rescan-items/<int:item_id>/registration.<fmt>")
def rescan_registration_image(item_id, fmt):
    """配准可视化：overlay=叠加（blend 滑杆）、diff=差异图、new=对齐后补扫图。"""
    it = db.one("SELECT * FROM rescan_items WHERE id=?", (item_id,))
    if not it:
        abort(404, "条目不存在")
    _rescan_batch_or_404(it["batch_id"])
    fmt = fmt.lower()
    if fmt not in ("overlay", "diff", "new", "jpg", "jpeg"):
        abort(404, "图像类型无效")
    if not it["file_id"]:
        abort(400, "该条目没有补扫文件")
    rf = db.one("SELECT * FROM rescan_files WHERE id=?", (it["file_id"],))
    reg = {}
    if it["reg_detail"]:
        try:
            reg = json.loads(it["reg_detail"])
        except ValueError:
            reg = {}
    batch = db.one("SELECT * FROM rescan_batches WHERE id=?", (it["batch_id"],))
    frame = rescan._target_frame_for_reg(db, batch["reel_id"], dict(it))
    if not frame or frame["placeholder"] or not frame["stored_path"]:
        abort(404, "无原图可比（缺帧占位条目没有叠加图）")
    w = max(200, min(1600, int(request.args.get("w", 900))))
    blend = max(0.0, min(1.0, float(request.args.get("blend", 0.5))))
    # 前端微调过程中可带实时参数预览（未落库）
    try:
        live_rot = int(request.args.get("rotation", reg.get("rotation", 0)))
        live_dx = int(round(float(request.args.get("dx_full", reg.get("dx_full", 0)))))
        live_dy = int(round(float(request.args.get("dy_full", reg.get("dy_full", 0)))))
    except (TypeError, ValueError):
        live_rot, live_dx, live_dy = reg.get("rotation", 0), reg.get("dx_full", 0), reg.get("dy_full", 0)
    reg = dict(reg)
    reg.update({"rotation": live_rot, "dx_full": live_dx, "dy_full": live_dy})
    from qc_core import register as regmod
    with Image_open(rf["stored_path"]) as new_img, Image_open(frame["stored_path"]) as ref_img:
        ref_im = imaging.apply_rotation(ref_img.convert("RGB"), frame["rotation"])
        new_im = new_img.convert("RGB")
        if fmt in ("diff",):
            data = regmod.diff_jpeg(ref_im, new_im, reg, out_w=w)
        elif fmt in ("new",):
            data = regmod.aligned_new(ref_im, new_im, reg, out_w=w)
        else:
            data = regmod.overlay_jpeg(ref_im, new_im, reg, out_w=w, blend=blend)
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


def Image_open(path):
    from PIL import Image
    return Image.open(path)


@app.route("/api/rescan-files/<int:file_id>/thumb")
def rescan_file_thumb(file_id):
    rf = db.one("SELECT * FROM rescan_files WHERE id=?", (file_id,))
    if not rf:
        abort(404)
    w = max(40, min(1600, int(request.args.get("w", 520))))
    data = imaging.make_thumb(rf["stored_path"], 0, w)
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


@app.route("/api/reels/<int:reel_id>/sample-rescan", methods=["POST"])
def sample_rescan(reel_id):
    """为演示卷生成一批补扫件（含改名文件与各类拦截样例）。"""
    from qc_core import sample_rescan
    reel = reel_or_404(reel_id)
    zip_bytes, manifest_bytes = sample_rescan.build_sample_rescan(db, reel_id, reel["reel_no"])
    entries = rescan.parse_backfill_manifest(manifest_bytes.decode("utf-8-sig"))
    name = "补扫批次（内置样例）"
    batch_id = rescan.import_batch(db, DATA, reel_id, reel["reel_no"], name,
                                   zip_bytes, entries,
                                   note="含改名回填、重拍替换及跨卷/错卷/重复占用拦截样例")
    return jsonify({"batch_id": batch_id, "detail": rescan.batch_detail(db, batch_id),
                    "state": state(reel_id)})


@app.route("/api/rescans/<int:batch_id>/export/batch.json")
def export_rescan_batch_json(batch_id):
    b = _rescan_batch_or_404(batch_id)
    payload = rescan.batch_export_json(db, batch_id)
    payload["exported_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    return send_file(buf, mimetype="application/json", as_attachment=True,
                     download_name="rescan_batch_%d.json" % batch_id)


@app.route("/api/rescans/<int:batch_id>/export/decisions.csv")
def export_rescan_decisions(batch_id):
    _rescan_batch_or_404(batch_id)
    data = rescan.decisions_csv(db, batch_id)
    return send_file(io.BytesIO(data), mimetype="text/csv", as_attachment=True,
                     download_name="rescan_decisions_%d.csv" % batch_id)


# ---------------------------------------------------------------- 导出

def export_rows(reel_id):
    frames = [dict(f) for f in db.frames(reel_id) if not f["excluded"]]
    versions = db.q(
        "SELECT frame_id, kind, source FROM frame_versions WHERE reel_id=? AND is_current=1",
        (reel_id,))
    vsrc = {v["frame_id"]: v for v in versions}
    source_rows = db.q(
        """SELECT fs.* FROM frame_sources fs
           WHERE fs.id IN (SELECT MAX(id) FROM frame_sources WHERE reel_id=? GROUP BY frame_id)""",
        (reel_id,))
    smap = {}
    for s in source_rows:
        region = {}
        try:
            region = json.loads(s["region"]) if s["region"] else {}
        except ValueError:
            pass
        if s["kind"] == "crop":
            provenance = ("拆分自 %s（第 %d/%d 段，%s 向切线）"
                          % (s["source_filename"], region.get("index", 0) + 1,
                             region.get("count", 1),
                             "竖" if region.get("axis") == "x" else "横"))
        elif s["kind"] == "stitch":
            provenance = "合并 %d 帧生成（%s）" % (
                region.get("count", 2), "左右拼接" if region.get("layout") == "h" else "上下拼接")
        else:
            provenance = ""
        smap[s["frame_id"]] = provenance
    rows = []
    for i, f in enumerate(frames):
        v = vsrc.get(f["id"])
        rows.append({
            "seq": i + 1,
            "reel_no": db.one("SELECT reel_no FROM reels WHERE id=?", (reel_id,))["reel_no"],
            "frame_no": f["frame_no"],
            "filename": f["filename"],
            "rotation": f["rotation"],
            "status": ("缺帧占位" if f["placeholder"] else
                       "需重拍" if f["reshoot"] else "合格"),
            "reshoot": "是" if (f["reshoot"] or f["placeholder"]) else "",
            "current_source": ("补扫回填" if v and v["kind"] in ("fill", "reshoot")
                               else "边界拆分/合并" if v and v["kind"] == "boundary"
                               else "原始扫描" if v else ""),
            "provenance": smap.get(f["id"]) or (v["source"] if v else ""),
            "note": f["note"],
        })
    return rows


@app.route("/api/reels/<int:reel_id>/export/manifest.<fmt>")
def export_manifest(reel_id, fmt):
    reel = reel_or_404(reel_id)
    rows = export_rows(reel_id)
    if fmt == "json":
        payload = {"reel_no": reel["reel_no"], "name": reel["name"],
                   "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "frame_count": len(rows), "frames": rows}
        buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        return send_file(buf, mimetype="application/json", as_attachment=True,
                         download_name="%s_manifest.json" % reel["reel_no"])
    buf = io.StringIO()
    default_cols = ["seq", "reel_no", "frame_no", "filename", "rotation", "status",
                    "reshoot", "current_source", "provenance", "note"]
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else default_cols)
    w.writeheader()
    w.writerows(rows)
    return send_file(io.BytesIO(buf.getvalue().encode("utf-8-sig")), mimetype="text/csv",
                     as_attachment=True, download_name="%s_manifest.csv" % reel["reel_no"])


@app.route("/api/reels/<int:reel_id>/export/reshoot.csv")
def export_reshoot(reel_id):
    reel = reel_or_404(reel_id)
    rows = [r for r in export_rows(reel_id) if r["reshoot"]]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["reel_no", "frame_no", "原因", "备注"])
    for r in rows:
        w.writerow([r["reel_no"], r["frame_no"], r["status"], r["note"]])
    return send_file(io.BytesIO(buf.getvalue().encode("utf-8-sig")), mimetype="text/csv",
                     as_attachment=True, download_name="%s_reshoot.csv" % reel["reel_no"])


@app.route("/api/reels/<int:reel_id>/export/contact.png")
def export_contact(reel_id):
    reel = reel_or_404(reel_id)
    frames = [dict(f) for f in db.frames(reel_id)]
    out = os.path.join(EXPORT_DIR, "contact_%d.png" % reel_id)
    imaging.contact_sheet(frames, None, out, reel["reel_no"])
    return send_file(out, mimetype="image/png", as_attachment=True,
                     download_name="%s_contact.png" % reel["reel_no"])


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
