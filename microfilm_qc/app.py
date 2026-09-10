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
from qc_core import analysis, imaging, sample_reel

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
FRAMES_DIR = os.path.join(DATA, "frames")
EXPORT_DIR = os.path.join(DATA, "exports")
os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

app = Flask(__name__)
db = DB(os.path.join(DATA, "qc.db"))

IMG_EXT = {".tif", ".tiff", ".jpg", ".jpeg"}


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
        db.add_frame(reel_id, position=position, frame_no=entry["frame_no"],
                     filename=os.path.basename(info.filename), stored_path=stored,
                     note=entry.get("note", ""), **fp)
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
    frames = []
    for f in db.frames(reel_id):
        d = {k: f[k] for k in ("id", "position", "frame_no", "filename", "note",
                               "brightness", "orient_score", "rotation",
                               "excluded", "placeholder", "reshoot")}
        frames.append(d)
    warnings = [dict(w) for w in db.q(
        "SELECT * FROM warnings WHERE reel_id=? ORDER BY resolved, id", (reel_id,))]
    return {
        "reel": {"id": reel["id"], "name": reel["name"], "reel_no": reel["reel_no"],
                 "finalized": reel["finalized"]},
        "frames": frames,
        "warnings": warnings,
        "can_undo": len(db.revisions(reel_id)) > 0,
        "checks": analysis.finalization_checks(db, reel_id),
    }


def mutate(reel_id, action, fn):
    """保存修订快照 -> 执行变更 -> 重算连续性。"""
    db.save_revision(reel_id, action)
    fn()
    renumber(reel_id)
    db.run("UPDATE reels SET finalized=0 WHERE id=?", (reel_id,))
    analysis.run_checks(db, reel_id)
    return jsonify(state(reel_id))


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
    db.run("DELETE FROM frames WHERE reel_id=?", (reel_id,))
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
    action = db.undo(reel_id)
    if action is None:
        return jsonify({"error": "没有可撤销的操作", "state": state(reel_id)}), 400
    db.run("UPDATE reels SET finalized=0 WHERE id=?", (reel_id,))
    analysis.run_checks(db, reel_id)
    return jsonify({"undone": action, "state": state(reel_id)})


@app.route("/api/reels/<int:reel_id>/finalize", methods=["POST"])
def finalize(reel_id):
    reel_or_404(reel_id)
    checks = analysis.finalization_checks(db, reel_id)
    if not checks["passed"]:
        return jsonify({"error": "定稿检查未通过", "checks": checks, "state": state(reel_id)}), 400
    db.run("UPDATE reels SET finalized=1 WHERE id=?", (reel_id,))
    return jsonify(state(reel_id))


# ---------------------------------------------------------------- 导出

def export_rows(reel_id):
    frames = [dict(f) for f in db.frames(reel_id) if not f["excluded"]]
    rows = []
    for i, f in enumerate(frames):
        rows.append({
            "seq": i + 1,
            "reel_no": db.one("SELECT reel_no FROM reels WHERE id=?", (reel_id,))["reel_no"],
            "frame_no": f["frame_no"],
            "filename": f["filename"],
            "rotation": f["rotation"],
            "status": ("缺帧占位" if f["placeholder"] else
                       "需重拍" if f["reshoot"] else "合格"),
            "reshoot": "是" if (f["reshoot"] or f["placeholder"]) else "",
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
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else
                       ["seq", "reel_no", "frame_no", "filename", "rotation", "status", "reshoot", "note"])
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
