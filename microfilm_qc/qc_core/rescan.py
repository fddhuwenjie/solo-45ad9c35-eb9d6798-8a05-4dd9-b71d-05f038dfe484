"""补扫回填核心：批次导入、清单校验拦截、接受/拒绝/改绑、版本来源关系。

拦截规则（导入时一次性判定，写明原因）：
  跨卷（清单 reel_no 与目标卷不符）
  一个原帧对应多个文件（同批次内多个条目指向同一原帧）
  同一文件重复占用（文件已被同批次其它条目占用 / 已被其它批次或当前有效图使用）
  目标不存在（清单帧号在卷内找不到）
另含：ZIP 中缺文件、目标不缺帧/未标重拍、内容重复提交（近重复）。
"""
import csv
import hashlib
import io
import json
import os
import re
import time
import zipfile

from . import imaging
from .analysis import DUP_HAMMING, BLANK_INK

IMG_EXT = {".tif", ".tiff", ".jpg", ".jpeg"}

STATUS_PENDING = "pending"
STATUS_BLOCKED = "blocked"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"

STATUS_LABEL = {
    STATUS_PENDING: "待处理",
    STATUS_BLOCKED: "已拦截",
    STATUS_ACCEPTED: "已接受",
    STATUS_REJECTED: "已拒绝",
}


# ---------------------------------------------------------------- 清单解析

def parse_backfill_manifest(text):
    """解析回填清单 CSV/JSON -> [{reel_no, frame_no, filename, note}]。"""
    text = (text or "").strip()
    if not text:
        return []
    if text[0] in "[{":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("items") or data.get("frames") or []
        out = []
        for d in data:
            out.append({
                "reel_no": str(d.get("reel_no", d.get("reel", "")) or "").strip(),
                "frame_no": str(d.get("frame_no", d.get("frame", "")) or "").strip(),
                "filename": str(d.get("filename", d.get("file", "")) or "").strip(),
                "note": str(d.get("note", d.get("备注", "")) or "").strip(),
            })
        return [r for r in out if r["frame_no"] or r["filename"]]
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        lower = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        rows.append({
            "reel_no": lower.get("reel_no") or lower.get("reel") or lower.get("卷号") or "",
            "frame_no": (lower.get("frame_no") or lower.get("frame")
                         or lower.get("原帧号") or lower.get("帧号") or ""),
            "filename": (lower.get("filename") or lower.get("file")
                         or lower.get("文件名") or ""),
            "note": lower.get("note") or lower.get("备注") or "",
        })
    return [r for r in rows if r["frame_no"] or r["filename"]]


# ---------------------------------------------------------------- 批次导入

def _target_frame(db, reel_id, frame_no):
    """帧号精确匹配（不做数字猜测，避免错绑）。"""
    return db.one("SELECT * FROM frames WHERE reel_id=? AND frame_no=? ORDER BY position",
                  (reel_id, str(frame_no)))


def _eligible(frame):
    """只匹配缺帧占位或已标记重拍的帧。"""
    return bool(frame) and (frame["placeholder"] or frame["reshoot"])


def import_batch(db, root, reel_id, reel_no, name, zip_bytes, entries, note=""):
    """落盘补扫文件、建条目并做拦截校验。返回 batch_id。"""
    bdir_rel = os.path.join("rescan")
    bcur = db.run("INSERT INTO rescan_batches(reel_id, name, note, created_at) VALUES(?,?,?,?)",
                  (reel_id, name, note, time.time()))
    batch_id = bcur.lastrowid
    bdir = os.path.join(root, bdir_rel, str(batch_id))
    os.makedirs(bdir, exist_ok=True)

    # 1) 读取 ZIP 内图像（basename 去目录、大小写不敏感索引）
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    zip_infos = {}
    dup_basenames = set()
    for info in zf.infolist():
        if info.is_dir():
            continue
        if os.path.splitext(info.filename)[1].lower() not in IMG_EXT:
            continue
        base = os.path.basename(info.filename)
        key = base.lower()
        if key in zip_infos:
            dup_basenames.add(key)
        else:
            zip_infos[key] = (base, info)

    # 同批次/已占用的文件名（大小写不敏感），防止目录覆盖
    used_names = {r["filename"].lower()
                  for r in db.q("""SELECT rf.filename FROM rescan_files rf
                                   JOIN rescan_batches rb ON rb.id=rf.batch_id
                                   WHERE rb.reel_id=?""", (reel_id,))}

    file_by_base = {}  # 小写 basename -> rescan_files.id
    md5_by_id = {}

    def store_file(base):
        key = base.lower()
        if key in file_by_base:
            return file_by_base[key]
        zname, info = zip_infos[key]
        raw = zf.read(info)
        md5 = hashlib.md5(raw).hexdigest()
        safe = zname
        if safe.lower() in used_names:
            root_name, ext = os.path.splitext(safe)
            safe = "%s_b%d%s" % (root_name, batch_id, ext)
        used_names.add(safe.lower())
        stored = os.path.join(bdir, safe)
        with open(stored, "wb") as fh:
            fh.write(raw)
        fp = imaging.fingerprint(stored)
        cur = db.run(
            """INSERT INTO rescan_files(batch_id, filename, stored_path, md5,
                                        width, height, phash, cvec, brightness, ink, orient_score)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, zname, stored, md5, fp["width"], fp["height"], fp["phash"],
             fp["cvec"], fp["brightness"], fp["ink"], fp["orient_score"]))
        fid = cur.lastrowid
        file_by_base[key] = fid
        md5_by_id[fid] = md5
        return fid

    # 2) 建条目（先全部 pending，再逐条校验）
    items = []
    for seq, e in enumerate(entries):
        base = os.path.basename(e.get("filename", "") or "")
        cur = db.run(
            """INSERT INTO rescan_items(batch_id, file_id, reel_no, frame_no, filename,
                                        target_frame_id, status, seq)
               VALUES(?,?,?,?,?,?,?,?)""",
            (batch_id, None, e.get("reel_no", ""), e.get("frame_no", ""), base,
             None, STATUS_PENDING, seq))
        item_id = cur.lastrowid
        items.append({"id": item_id, "reel_no": e.get("reel_no", ""),
                      "frame_no": e.get("frame_no", ""), "filename": base,
                      "note": e.get("note", ""), "file_id": None})

    # 3) 校验
    base_claims = {}   # 小写 basename -> [item_id...]
    target_claims = {}  # frame_no(str) -> [item_id...]
    for it in items:
        if it["filename"]:
            base_claims.setdefault(it["filename"].lower(), []).append(it["id"])
        if it["frame_no"]:
            target_claims.setdefault(str(it["frame_no"]), []).append(it["id"])

    # 卷内既有文件指纹（含历史版本与其它批次补扫件）
    occupied_md5 = _occupied_md5_map(db, reel_id, batch_id)

    blocked_items = []
    for it in items:
        reason = _validate_item(db, it, reel_id, reel_no, batch_id, zip_infos,
                                dup_basenames, base_claims, target_claims,
                                store_file, occupied_md5)
        # 文件存在即登记 file_id/target（便于改绑后直接接受），拦截原因单独保留
        if reason:
            db.run("UPDATE rescan_items SET status=?, block_reason=?, file_id=COALESCE(?,file_id),"
                   " target_frame_id=COALESCE(?,target_frame_id) WHERE id=?",
                   (STATUS_BLOCKED, reason, it.get("file_id"), it.get("target_frame_id"),
                    it["id"]))
            it["status"] = STATUS_BLOCKED
            it["block_reason"] = reason
            blocked_items.append(it)
        else:
            db.run("UPDATE rescan_items SET file_id=?, target_frame_id=?, status=? WHERE id=?",
                   (it["file_id"], it["target_frame_id"], STATUS_PENDING, it["id"]))
            it["status"] = STATUS_PENDING

    # 4) ZIP 中清单未引用的文件：落盘并登记为拦截条目（可改绑）
    claimed_keys = {it["filename"].lower() for it in items if it["filename"]}
    orphans = sorted(k for k in zip_infos if k not in claimed_keys)
    for key in orphans:
        base = zip_infos[key][0]
        fid = store_file(base)
        reason = "清单外文件：回填清单未引用 %s，请核对后改绑到对应原帧或忽略" % base
        cur = db.run(
            """INSERT INTO rescan_items(batch_id, file_id, reel_no, frame_no, filename,
                                        target_frame_id, status, block_reason, seq)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (batch_id, fid, reel_no, "", base, None, STATUS_BLOCKED, reason, len(items)))
        items.append({"id": cur.lastrowid, "reel_no": reel_no, "frame_no": "",
                      "filename": base, "file_id": fid, "status": STATUS_BLOCKED,
                      "block_reason": reason})

    return batch_id


def _validate_item(db, it, reel_id, reel_no, batch_id, zip_infos, dup_basenames,
                   base_claims, target_claims, store_file, occupied_md5):
    """返回拦截原因字符串；None 表示可进入待处理。同时回填 file_id/target_frame_id。"""
    base = it["filename"]
    # a) ZIP 中缺文件
    if not base:
        return "清单未提供文件名，无法定位补扫文件"
    key = base.lower()
    if key not in zip_infos:
        return "补扫 ZIP 中找不到文件：%s（改名？请核对文件名或使用改绑）" % base
    if key in dup_basenames:
        return "ZIP 内存在同名文件 %s，路径有歧义，已拦截" % base

    fid = store_file(base)
    it["file_id"] = fid
    rf = db.one("SELECT * FROM rescan_files WHERE id=?", (fid,))

    # b) 同一文件在本批次被多条清单行占用
    claimers = [i for i in base_claims.get(key, []) if i != it["id"]]
    if claimers:
        other = db.one("SELECT frame_no FROM rescan_items WHERE id=?", (claimers[0],))
        return ("同一文件被重复占用：%s 同时对应原帧 No.%s 与 No.%s"
                % (base, other["frame_no"], it["frame_no"]))

    # c) 跨卷
    listed_reel = (it.get("reel_no") or "").strip()
    if listed_reel and reel_no and listed_reel != reel_no:
        return "跨卷拦截：清单卷号 %s 与当前卷 %s 不符" % (listed_reel, reel_no)

    # d) 目标不存在
    if not it["frame_no"]:
        return "清单缺少原帧号，无法匹配回填目标"
    frame = _target_frame(db, reel_id, it["frame_no"])
    if not frame:
        return "目标不存在：卷 %s 内找不到原帧 No.%s" % (reel_no, it["frame_no"])
    it["target_frame_id"] = frame["id"]

    # e) 一个原帧对应多个文件（本批次内）
    multi = [i for i in target_claims.get(str(it["frame_no"]), []) if i != it["id"]]
    if multi:
        names = [r["filename"] for r in db.q(
            "SELECT filename FROM rescan_items WHERE id IN (%s)" %
            ",".join("?" * len(multi)), multi)]
        return ("一个原帧对应多个文件：No.%s 同时有 %s，请改绑到正确帧或拒绝多余项"
                % (it["frame_no"], "、".join([base] + names)))

    # f) 目标状态不符：只接受占位/重拍帧
    if not _eligible(frame):
        return ("目标帧 No.%s 既非缺帧占位也未标记重拍，禁止覆盖正常帧"
                % it["frame_no"])

    # g) 同一文件重复占用：与其它批次/当前有效图字节相同
    reason = _same_file_occupy_reason(db, rf, reel_id, batch_id, base, occupied_md5)
    if reason:
        return reason

    # h) 内容重复提交：与卷内任一有效图像近重复
    return _content_dup_reason(db, rf, reel_id, base)


def _occupied_md5_map(db, reel_id, batch_id):
    """卷内已占用文件的 MD5 -> 文件名（含其它批次补扫件与当前有效图，不含本批次）。"""
    occupied_md5 = {}
    for r in db.q("SELECT md5, filename FROM rescan_files WHERE md5!='' AND batch_id!=?",
                  (batch_id,)):
        occupied_md5.setdefault(r["md5"], r["filename"])
    for r in db.q("SELECT stored_path, filename FROM frames WHERE reel_id=? AND placeholder=0",
                  (reel_id,)):
        p = r["stored_path"]
        if p and os.path.exists(p):
            occupied_md5.setdefault(_md5_file(p), r["filename"])
    return occupied_md5


def _same_file_occupy_reason(db, rf, reel_id, batch_id, base, occupied_md5=None):
    """同一文件重复占用：与其它批次补扫件或当前有效图字节完全相同。返回原因或 None。"""
    if occupied_md5 is None:
        occupied_md5 = _occupied_md5_map(db, reel_id, batch_id)
    if rf["md5"] in occupied_md5:
        return "同一文件重复占用：%s 与已提交/当前有效文件 %s 完全相同" % (
            base, occupied_md5[rf["md5"]])
    return None


def _content_dup_reason(db, rf, reel_id, base):
    """内容重复提交：与卷内任一有效图像近重复（感知哈希距离过近）。返回原因或 None。"""
    if not (rf["phash"] and (rf["ink"] is None or rf["ink"] >= BLANK_INK)):
        return None
    newhash = int(rf["phash"], 16)
    for r in db.q("""SELECT id, frame_no, phash, ink FROM frames
                     WHERE reel_id=? AND placeholder=0 AND phash!=''""", (reel_id,)):
        if r["ink"] is not None and r["ink"] < BLANK_INK:
            continue
        dist = imaging.hamming(newhash, int(r["phash"], 16))
        if dist <= DUP_HAMMING:
            return ("重复提交：%s 与卷内 No.%s 当前有效图内容几乎相同（哈希距离 %d）"
                    % (base, r["frame_no"], dist))
    return None


def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 批次查询

def list_batches(db, reel_id):
    rows = db.q("SELECT * FROM rescan_batches WHERE reel_id=? ORDER BY id DESC", (reel_id,))
    out = []
    for r in rows:
        cnt = db.one(
            """SELECT
               SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending,
               SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) blocked,
               SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END) accepted,
               SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) rejected,
               COUNT(*) total
               FROM rescan_items WHERE batch_id=?""", (r["id"],))
        out.append({
            "id": r["id"], "name": r["name"], "note": r["note"],
            "created_at": r["created_at"],
            "pending": cnt["pending"] or 0, "blocked": cnt["blocked"] or 0,
            "accepted": cnt["accepted"] or 0, "rejected": cnt["rejected"] or 0,
            "total": cnt["total"] or 0,
        })
    return out


def _orient_label(frame):
    s = frame["orient_score"] if frame else 0
    return "横排" if s >= 0 else "竖排"


def _mad(a, b):
    if not a or not b:
        return None
    return round(imaging.cvec_mad(a, b), 2)


def batch_detail(db, batch_id):
    b = db.one("SELECT * FROM rescan_batches WHERE id=?", (batch_id,))
    if not b:
        return None
    reel_id = b["reel_id"]
    frames = [dict(r) for r in db.frames(reel_id)]
    by_id = {f["id"]: f for f in frames}

    items = []
    for r in db.q("SELECT * FROM rescan_items WHERE batch_id=? ORDER BY seq, id", (batch_id,)):
        d = dict(r)
        rf = db.one("SELECT * FROM rescan_files WHERE id=?", (r["file_id"],)) if r["file_id"] else None
        target = by_id.get(r["target_frame_id"]) if r["target_frame_id"] else None
        # 未解析目标时，也尝试按帧号找（用于展示）
        if not target and r["frame_no"]:
            target = _target_frame(db, reel_id, r["frame_no"])

        entry = {
            "id": d["id"], "seq": d["seq"], "reel_no": d["reel_no"],
            "frame_no": d["frame_no"], "filename": d["filename"],
            "status": d["status"], "status_label": STATUS_LABEL.get(d["status"], d["status"]),
            "block_reason": d["block_reason"], "decision_note": d["decision_note"],
            "decided_at": d["decided_at"], "file_id": d["file_id"],
            "target_frame_id": target["id"] if target else None,
        }
        if rf:
            entry["new"] = {
                "file_id": rf["id"], "filename": rf["filename"],
                "width": rf["width"], "height": rf["height"],
                "brightness": round(rf["brightness"], 1),
                "orient": _orient_label(rf), "orient_score": round(rf["orient_score"], 0),
            }
        if target:
            entry["old"] = {
                "frame_id": target["id"], "frame_no": target["frame_no"],
                "filename": target["filename"], "placeholder": target["placeholder"],
                "reshoot": target["reshoot"],
                "width": target["width"], "height": target["height"],
                "brightness": round(target["brightness"], 1),
                "orient": _orient_label(target), "orient_score": round(target["orient_score"], 0),
            }
            # 连续性：与相邻帧（占位帧无向量则跳过）
            i = next((k for k, f in enumerate(frames) if f["id"] == target["id"]), None)
            cont = {}
            if i is not None:
                prev = frames[i - 1] if i > 0 else None
                nxt = frames[i + 1] if i + 1 < len(frames) else None
                cont["prev_no"] = prev["frame_no"] if prev else None
                cont["next_no"] = nxt["frame_no"] if nxt else None
                if rf and rf["cvec"]:
                    cont["new_prev_mad"] = _mad(rf["cvec"], prev["cvec"]) if prev and not prev["placeholder"] else None
                    cont["new_next_mad"] = _mad(rf["cvec"], nxt["cvec"]) if nxt and not nxt["placeholder"] else None
                if not target["placeholder"] and target["cvec"]:
                    cont["old_prev_mad"] = _mad(target["cvec"], prev["cvec"]) if prev and not prev["placeholder"] else None
                    cont["old_next_mad"] = _mad(target["cvec"], nxt["cvec"]) if nxt and not nxt["placeholder"] else None
                if rf and not target["placeholder"]:
                    cont["old_new_mad"] = _mad(rf["cvec"], target["cvec"])
                if rf:
                    cont["brightness_delta_prev"] = (round(rf["brightness"] - prev["brightness"], 1)
                                                     if prev and not prev["placeholder"] else None)
            entry["continuity"] = cont
        items.append(entry)

    # 清单外文件：任何条目都未引用（被拦截条目引用的文件不算清单外）
    unused = [r["filename"] for r in db.q(
        """SELECT rf.filename FROM rescan_files rf
           WHERE rf.batch_id=?
             AND NOT EXISTS (SELECT 1 FROM rescan_items ri
                             WHERE ri.batch_id=rf.batch_id AND ri.file_id=rf.id)
           ORDER BY rf.filename""",
        (batch_id,))]

    cnt = {"pending": 0, "blocked": 0, "accepted": 0, "rejected": 0}
    for it in items:
        cnt[it["status"]] = cnt.get(it["status"], 0) + 1
    return {
        "batch": {"id": b["id"], "reel_id": reel_id, "name": b["name"], "note": b["note"],
                  "created_at": b["created_at"]},
        "items": items, "unused_files": unused, "counts": cnt,
    }


# ---------------------------------------------------------------- 接受 / 拒绝 / 改绑

def _refresh_eligibility(db, item):
    """操作前再校验，防止过期批次误覆盖。返回 (frame, reason)。"""
    if not item["file_id"]:
        return None, "条目缺少补扫文件"
    frame = db.one("SELECT * FROM frames WHERE id=?", (item["target_frame_id"],))
    if not frame:
        return None, "目标帧已不存在"
    if not _eligible(frame):
        return None, "目标帧 No.%s 已不是缺帧占位/重拍状态" % frame["frame_no"]
    # 同帧不能被本批次另一个待处理/已接受条目再占用
    other = db.one(
        """SELECT id FROM rescan_items
           WHERE batch_id=? AND target_frame_id=? AND id!=?
             AND status IN ('pending','accepted')""",
        (item["batch_id"], frame["id"], item["id"]))
    if other:
        return None, "原帧 No.%s 已被本批次另一条目占用" % frame["frame_no"]
    return frame, None


def accept_item(db, item_id, note=""):
    """接受单个条目。返回 (reel_id, frame_id, extra)；失败抛 ValueError。"""
    it = db.one("SELECT * FROM rescan_items WHERE id=?", (item_id,))
    if not it:
        raise ValueError("条目不存在")
    if it["status"] in (STATUS_ACCEPTED,):
        raise ValueError("该条目已接受")
    if it["status"] == STATUS_BLOCKED:
        raise ValueError("已拦截条目不能直接接受，请改绑后再接受或予以拒绝")
    if it["status"] == STATUS_REJECTED:
        raise ValueError("已拒绝的条目请改绑后再接受，或重新导入")
    frame, err = _refresh_eligibility(db, it)
    if err:
        raise ValueError(err)
    rf = db.one("SELECT * FROM rescan_files WHERE id=?", (it["file_id"],))
    if not rf:
        raise ValueError("补扫文件记录缺失")

    old_current = db.one(
        "SELECT * FROM frame_versions WHERE frame_id=? AND is_current=1", (frame["id"],))
    if old_current:
        db.run("UPDATE frame_versions SET is_current=0 WHERE id=?", (old_current["id"],))
    else:
        # 老数据补种子：保留原文件为历史版本
        if not frame["placeholder"] and frame["stored_path"]:
            db.add_version(frame["id"], frame["reel_id"], "original",
                           frame["filename"], frame["stored_path"],
                           source="初次导入", is_current=0)

    kind = "reshoot" if frame["reshoot"] and not frame["placeholder"] else "fill"
    db.add_version(frame["id"], frame["reel_id"], kind, rf["filename"], rf["stored_path"],
                   source="补扫批次 #%s" % it["batch_id"],
                   batch_id=it["batch_id"], item_id=it["id"], is_current=1)

    db.run(
        """UPDATE frames SET filename=?, stored_path=?, width=?, height=?, phash=?, cvec=?,
                             brightness=?, ink=?, orient_score=?, rotation=0,
                             placeholder=0, reshoot=0 WHERE id=?""",
        (rf["filename"], rf["stored_path"], rf["width"], rf["height"], rf["phash"],
         rf["cvec"], rf["brightness"], rf["ink"], rf["orient_score"], frame["id"]))

    db.run("UPDATE rescan_items SET status=?, decision_note=?, decided_at=? WHERE id=?",
           (STATUS_ACCEPTED, note, time.time(), item_id))

    extra = {"rescan": [{
        "op": "accept", "item_id": item_id, "frame_id": frame["id"],
        "old_version_id": old_current["id"] if old_current else None,
        "old_frame": {c: frame[c] for c in
                      ("placeholder", "reshoot", "filename", "stored_path", "width", "height",
                       "phash", "cvec", "brightness", "ink", "orient_score", "rotation")},
    }]}
    return frame["reel_id"], frame["id"], extra


def reject_item(db, item_id, note=""):
    it = db.one("SELECT * FROM rescan_items WHERE id=?", (item_id,))
    if not it:
        raise ValueError("条目不存在")
    batch = db.one("SELECT * FROM rescan_batches WHERE id=?", (it["batch_id"],))
    if not batch:
        raise ValueError("条目所属批次不存在")
    db.run("UPDATE rescan_items SET status=?, decision_note=?, decided_at=? WHERE id=?",
           (STATUS_REJECTED, note, time.time(), item_id))
    extra = {"rescan": [{"op": "decide", "item_id": item_id,
                         "status": it["status"], "block_reason": it["block_reason"],
                         "decision_note": it["decision_note"]}]}
    # 返回值第一项必须是 reel_id（不能用 batch_id 代替），否则修订与撤销会挂错卷
    return batch["reel_id"], None, extra


def rebind_item(db, item_id, new_frame_no, note=""):
    """改绑到同卷另一个占位/重拍帧。"""
    it = db.one("SELECT * FROM rescan_items WHERE id=?", (item_id,))
    if not it:
        raise ValueError("条目不存在")
    batch = db.one("SELECT * FROM rescan_batches WHERE id=?", (it["batch_id"],))
    target = _target_frame(db, batch["reel_id"], new_frame_no)
    if not target:
        raise ValueError("卷内找不到帧 No.%s" % new_frame_no)
    if not _eligible(target):
        raise ValueError("No.%s 既非缺帧占位也未标记重拍，不能改绑" % new_frame_no)
    clash = db.one(
        """SELECT id FROM rescan_items
           WHERE batch_id=? AND target_frame_id=? AND id!=?
             AND status IN ('pending','accepted')""",
        (it["batch_id"], target["id"], item_id))
    if clash:
        raise ValueError("No.%s 已被本批次另一条目占用" % new_frame_no)

    # 改绑必须重新执行文件占用校验：因“同一文件重复占用/内容重复”被拦截的文件，
    # 不能仅靠改绑变成 pending，再被另一帧接受。
    if not it["file_id"]:
        raise ValueError("该条目没有补扫文件，无法改绑")
    rf = db.one("SELECT * FROM rescan_files WHERE id=?", (it["file_id"],))
    if not rf:
        raise ValueError("补扫文件记录缺失，无法改绑")
    reason = _same_file_occupy_reason(db, rf, batch["reel_id"], it["batch_id"],
                                      rf["filename"])
    if not reason:
        reason = _content_dup_reason(db, rf, batch["reel_id"], rf["filename"])
    if reason:
        raise ValueError(reason + "（文件占用问题不能通过改绑绕过）")

    old_target = it["target_frame_id"]
    old_status, old_reason = it["status"], it["block_reason"]
    db.run(
        "UPDATE rescan_items SET target_frame_id=?, frame_no=?, status=?, block_reason=?, decision_note=?, decided_at=? WHERE id=?",
        (target["id"], new_frame_no, STATUS_PENDING, "", note, time.time(), item_id))
    extra = {"rescan": [{"op": "decide", "item_id": item_id, "status": old_status,
                         "block_reason": old_reason, "decision_note": it["decision_note"],
                         "old_target_frame_id": old_target, "old_frame_no": it["frame_no"]}]}
    return batch["reel_id"], None, extra


def accept_clean(db, batch_id):
    """批量接受全部无冲突待处理项。返回 (reel_id, [frame_ids], extra)。"""
    items = db.q(
        "SELECT id FROM rescan_items WHERE batch_id=? AND status='pending' ORDER BY seq, id",
        (batch_id,))
    changed, ops, errors = [], [], []
    reel_id = None
    for r in items:
        try:
            rid, fid, extra = accept_item(db, r["id"], note="批量接受无冲突项")
            reel_id = rid
            changed.append(fid)
            ops.extend(extra["rescan"])
        except ValueError as ex:
            errors.append({"item_id": r["id"], "reason": str(ex)})
    if reel_id is None:
        b = db.one("SELECT reel_id FROM rescan_batches WHERE id=?", (batch_id,))
        reel_id = b["reel_id"] if b else None
    return reel_id, changed, {"rescan": ops}, errors


# ---------------------------------------------------------------- 导出

def batch_export_json(db, batch_id):
    detail = batch_detail(db, batch_id)
    return {
        "batch": detail["batch"],
        "counts": detail["counts"],
        "unused_files": detail["unused_files"],
        "items": [{k: v for k, v in it.items() if k in (
            "seq", "reel_no", "frame_no", "filename", "status", "status_label",
            "block_reason", "decision_note", "old", "new", "continuity")}
            for it in detail["items"]],
    }


def decisions_csv(db, batch_id):
    detail = batch_detail(db, batch_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["reel_no", "原帧号", "补扫文件", "处理结果", "拦截/备注原因", "处理时间"])
    for it in detail["items"]:
        w.writerow([
            it["reel_no"], it["frame_no"], it["filename"],
            it["status_label"], it["block_reason"] or it["decision_note"] or "",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(it["decided_at"]))
            if it["decided_at"] else "",
        ])
    return buf.getvalue().encode("utf-8-sig")


def seed_original_versions(db, reel_id=None):
    """为老数据中缺少版本记录的实体帧补建 original 版本（幂等）。"""
    sql = "SELECT * FROM frames WHERE placeholder=0 AND stored_path!=''"
    args = ()
    if reel_id is not None:
        sql += " AND reel_id=?"
        args = (reel_id,)
    for f in db.q(sql, args):
        exists = db.one("SELECT 1 FROM frame_versions WHERE frame_id=?", (f["id"],))
        if not exists:
            db.add_version(f["id"], f["reel_id"], "original", f["filename"],
                           f["stored_path"], source="初次导入", is_current=1)
