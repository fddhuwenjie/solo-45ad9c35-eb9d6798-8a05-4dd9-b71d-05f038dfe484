"""二次验收（抽查复核）核心。

一卷修订完成后，复核员不看旧告警与处置痕迹，按可复现种子在首段/中段/末段
均衡随机抽样，并强制纳入曾经重拍、回填、拆分或人工越过配准门槛的图像；
去重后排除缺帧占位。抽样开始即锁定每张图的当前版本（frame_versions 行 +
文件路径 + 旋转 + MD5）：换图后旧审阅不被覆盖，只把受影响记录作废（void），
审阅历史（review_judgements）永久保留。

轮次链（chain）：首抽未过时可“自动加抽”——加抽是链上的新一轮，复用同链 id；
“退回整卷”结束整链。只有最新一轮以“通过”结论结束、且锁定版本与当前有效
版本一致，才算有效通过结论，定稿接口必须关联到它。
"""
import csv
import hashlib
import io
import json
import math
import os
import random
import time

# 强制纳入标签
TAG_RESHOOT = "reshoot"      # 曾经重拍（接受过重拍替换版本）
TAG_FILL = "fill"            # 曾经回填（补扫填充缺帧/占位）
TAG_SPLIT = "split"          # 拆分/合并等边界修订产物
TAG_REG_FORCE = "reg_force"  # 人工越过配准门槛强制接受

TAG_LABEL = {
    TAG_RESHOOT: "重拍", TAG_FILL: "回填", TAG_SPLIT: "拆分/合并",
    TAG_REG_FORCE: "越过配准门槛",
}

SEG_HEAD, SEG_MID, SEG_TAIL = "head", "middle", "tail"
SEG_LABEL = {SEG_HEAD: "首段", SEG_MID: "中段", SEG_TAIL: "末段", "forced": "强制加入"}

ST_OPEN, ST_PASSED, ST_FAILED, ST_RETURNED = "open", "passed", "failed", "returned"
STATUS_LABEL = {ST_OPEN: "复核中", ST_PASSED: "通过", ST_FAILED: "未通过", ST_RETURNED: "已退回整卷"}

IT_PENDING, IT_PASS, IT_FAIL, IT_VOID = "pending", "pass", "fail", "void"

REVIEW_DIMS = ["clarity", "crop", "orientation", "blemish", "missing"]
DIM_LABEL = {"clarity": "清晰度", "crop": "裁边", "orientation": "朝向",
             "blemish": "污损", "missing": "内容缺失"}

DEFAULT_FAIL_LIMIT = 0       # 默认失败门限：不允许失败
SETTING_FAIL_LIMIT = "review_fail_limit"
MAX_REMARKS = 1000


# ---------------------------------------------------------------- 可复现抽样

def _seed_stream(seed_text, salt):
    """由种子文本派生独立的随机流（不同用途用不同 salt，互不干扰）。"""
    h = hashlib.sha256(("review:%s:%s" % (salt, seed_text)).encode("utf-8")).hexdigest()
    return random.Random(int(h[:16], 16))


def thirds(n):
    """把长度 n 尽量均分成三段 (start,end) 的索引区间。"""
    if n <= 0:
        return []
    if n <= 3:
        return [(i, i + 1) for i in range(n)]
    base, rem = divmod(n, 3)
    bounds, start = [], 0
    for k in range(3):
        size = base + (1 if k < rem else 0)
        bounds.append((start, start + size))
        start += size
    return bounds


def _distribute(total, n_segments):
    """把 total 个名额尽量均匀分到三段；total>=n_segments 时保证每段至少 1。"""
    if n_segments <= 0:
        return []
    total = max(0, min(total, sum(1 for _ in range(10 ** 6))))
    if total <= 0:
        return [0] * n_segments
    if total >= n_segments:
        base, rem = divmod(total, n_segments)
        return [base + (1 if k < rem else 0) for k in range(n_segments)]
    return [1 if k < total else 0 for k in range(n_segments)]


def select_sample(pool, count, ratio, mode, seed, forced_ids, exclude_ids=()):
    """纯函数：按种子在首/中/末段分别抽样，强制集去重后并入。

    pool: [frame dict]，已按 position 排好、已排除占位/剔除（抽样总体）。
    返回 {items: [{frame_id, segment, selected_by}], counts: {...}}。
    段内随机用同一条种子随机流依次洗牌三段，保证可复现。
    """
    n = len(pool)
    exclude = set(exclude_ids or ())
    forced_ids = set(forced_ids or ())
    if mode == "ratio":
        count = int(math.ceil(max(0.0, min(1.0, ratio)) * n))
    count = max(0, min(int(count), n))

    chosen = {}   # frame_id -> {segment, selected_by}
    seg_ranges = thirds(n)
    seg_names = [SEG_HEAD, SEG_MID, SEG_TAIL]
    seg_hits = {SEG_HEAD: 0, SEG_MID: 0, SEG_TAIL: 0}
    seg_of = {}
    for k, (a, b) in enumerate(seg_ranges):
        for f in pool[a:b]:
            seg_of[f["id"]] = seg_names[k]
    quota = _distribute(count, len(seg_ranges))
    # 段内名额在该段可抽帧中按种子洗牌取得；抽不满的名额进入余量池
    rng = _seed_stream(seed, "segments")
    spill = []
    for (a, b), seg_name, q in zip(seg_ranges, seg_names, quota):
        avail = [f for f in pool[a:b] if f["id"] not in exclude]
        order = list(avail)
        rng.shuffle(order)
        take = min(q, len(order))
        for f in order[:take]:
            chosen[f["id"]] = {"frame_id": f["id"], "segment": seg_name,
                               "selected_by": "random"}
            seg_hits[seg_name] += 1
        spill.extend(f for f in order[take:] if f["id"] not in exclude)

    # 余量：某段可抽帧不足时，从全卷未抽中帧里按种子顺延补齐（统计计入原段）
    need = count - len(chosen)
    if need > 0 and spill:
        rng2 = _seed_stream(seed, "overflow")
        rng2.shuffle(spill)
        for f in spill[:need]:
            seg = seg_of.get(f["id"], SEG_MID)
            chosen[f["id"]] = {"frame_id": f["id"], "segment": seg,
                               "selected_by": "random"}
            seg_hits[seg] += 1

    # 强制加入（只从非占位总体中取；去重：已被随机抽中的合并标记为 both）
    forced_hits = 0
    pool_by_id = {f["id"]: f for f in pool}
    frng = _seed_stream(seed, "forced")
    forced_order = sorted((fid for fid in forced_ids if fid in pool_by_id),
                          key=lambda x: x)
    frng.shuffle(forced_order)
    for fid in forced_order:
        if fid in chosen:
            chosen[fid]["selected_by"] = "both"
            continue
        chosen[fid] = {"frame_id": fid, "segment": "forced", "selected_by": "forced"}
        forced_hits += 1

    n_random = sum(1 for v in chosen.values()
                   if v["selected_by"] in ("random", "both"))
    return {
        "items": list(chosen.values()),
        "counts": {"random": n_random,
                   "forced": forced_hits,
                   "head": seg_hits[SEG_HEAD], "middle": seg_hits[SEG_MID],
                   "tail": seg_hits[SEG_TAIL], "pool": n, "target": count},
    }


# ---------------------------------------------------------------- 强制纳入判定

def forced_tags_for_frame(db, reel_id, frame_id):
    """判定某帧当前有效图为何应强制纳入（按版本史 + 强制接受记录）。

    - 重拍：接受过 kind='reshoot' 的版本（重拍替换）
    - 回填：接受过 kind='fill' 的版本（占位回填）
    - 拆分：当前版本来自边界拆分/合并（kind='boundary'）
    - 越过配准门槛：对应补扫条目带 force_reason 接受
    """
    tags = set()
    for r in db.q("SELECT DISTINCT kind FROM frame_versions WHERE frame_id=?", (frame_id,)):
        if r["kind"] == "reshoot":
            tags.add(TAG_RESHOOT)
        elif r["kind"] == "fill":
            tags.add(TAG_FILL)
    cur = db.one("SELECT * FROM frame_versions WHERE frame_id=? AND is_current=1", (frame_id,))
    if cur and cur["kind"] == "boundary":
        tags.add(TAG_SPLIT)
    if cur and cur["item_id"]:
        it = db.one("SELECT force_reason FROM rescan_items WHERE id=?", (cur["item_id"],))
        if it and (it["force_reason"] or "").strip():
            tags.add(TAG_REG_FORCE)
    return tags


# ---------------------------------------------------------------- MD5

def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 轮次管理

def _parse_int(body, key, default, lo=None, hi=None, name=None):
    raw = body.get(key)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise ValueError("%s必须是整数" % (name or key))
    if lo is not None and v < lo:
        raise ValueError("%s不能小于 %s" % (name or key, lo))
    if hi is not None and v > hi:
        raise ValueError("%s不能大于 %s" % (name or key, hi))
    return v


def validate_params(db, body):
    """校验并归一化抽样参数。返回 dict。"""
    reviewer = str(body.get("reviewer", "") or "").strip()
    if not reviewer:
        raise ValueError("请填写复核人")
    mode = str(body.get("mode", "count") or "count").strip()
    if mode not in ("count", "ratio"):
        raise ValueError("抽样方式只能是 count（数量）或 ratio（占比）")
    count = ratio = 0
    if mode == "count":
        count = _parse_int(body, "count", 0, lo=1, name="抽取数量")
    else:
        try:
            ratio = float(body.get("ratio", 0))
        except (TypeError, ValueError):
            raise ValueError("抽取占比必须是 0-1 之间的数")
        if not (0 < ratio <= 1):
            raise ValueError("抽取占比必须在 (0, 1] 之间")
    seed = str(body.get("seed", "") or "").strip()
    if not seed:
        seed = hashlib.sha1(os.urandom(16)).hexdigest()[:12]
    fail_limit = body.get("fail_limit")
    if fail_limit is None or str(fail_limit).strip() == "":
        fail_limit = int(db.get_setting(SETTING_FAIL_LIMIT, DEFAULT_FAIL_LIMIT) or 0)
    else:
        try:
            fail_limit = int(fail_limit)
        except (TypeError, ValueError):
            raise ValueError("失败门限必须是非负整数")
    if fail_limit < 0:
        raise ValueError("失败门限不能为负")
    return {"reviewer": reviewer, "mode": mode, "count": count, "ratio": ratio,
            "seed": seed, "fail_limit": fail_limit}


def _sampling_pool(db, reel_id):
    """抽样总体：非占位、非剔除、有有效图文件的帧（缺图占位天然排除）。"""
    frames = [dict(f) for f in db.frames(reel_id)
              if not f["placeholder"] and not f["excluded"] and f["stored_path"]
              and os.path.exists(f["stored_path"])]
    return frames


def _excluded_reviewed_ids(db, reel_id, chain_id):
    """加抽时不再重复抽同链上历次已判定（pass/fail）且未作废的帧。"""
    rows = db.q(
        """SELECT ri.frame_id FROM review_items ri
           JOIN review_rounds rr ON rr.id=ri.round_id
           WHERE rr.chain_id=? AND rr.reel_id=?
             AND ri.status IN ('pass','fail')""",
        (chain_id, reel_id))
    return {r["frame_id"] for r in rows if r["frame_id"] is not None}


def create_round(db, reel_id, params, parent=None):
    """创建一轮抽样并锁定版本。parent 给定时为该链的加抽轮。"""
    if db.one("SELECT id FROM review_rounds WHERE reel_id=? AND status='open'",
              (reel_id,)):
        raise ValueError("该卷已有复核中的抽查轮次，请先结束（通过/加抽/退回）")
    pool = _sampling_pool(db, reel_id)
    if not pool:
        raise ValueError("卷内没有可抽的有效图像（缺图占位不参与抽样）")

    if parent is None:
        chain_id = None  # 插入后取 id
        seq = 1
    else:
        if parent["status"] != ST_FAILED:
            raise ValueError("只有未通过的轮次才能发起加抽")
        chain_id, seq = parent["chain_id"], parent["seq"] + 1

    # 强制集：总体中所有带强制标签的帧
    forced_map = {f["id"]: forced_tags_for_frame(db, reel_id, f["id"]) for f in pool}
    forced_ids = {fid for fid, tags in forced_map.items() if tags}
    exclude = _excluded_reviewed_ids(db, reel_id, chain_id) if chain_id is not None else set()

    result = select_sample(pool, params["count"], params["ratio"], params["mode"],
                           params["seed"], forced_ids, exclude)
    picked = {v["frame_id"]: v for v in result["items"]}
    if not picked:
        raise ValueError("抽样结果为空（可能已无可抽的新帧），请改为退回整卷")

    cur = db.run(
        """INSERT INTO review_rounds(reel_id, parent_id, chain_id, seq, reviewer, seed,
                                     mode, sample_count, sample_ratio, fail_limit, status,
                                     pool_size, n_random, n_forced, n_head, n_middle, n_tail,
                                     created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (reel_id, parent["id"] if parent else None, chain_id or 0, seq,
         params["reviewer"], params["seed"], params["mode"], params["count"],
         params["ratio"], params["fail_limit"], ST_OPEN, result["counts"]["pool"],
         result["counts"]["random"], result["counts"]["forced"],
         result["counts"]["head"], result["counts"]["middle"], result["counts"]["tail"],
         time.time()))
    round_id = cur.lastrowid
    if parent is None:
        db.run("UPDATE review_rounds SET chain_id=? WHERE id=?", (round_id, round_id))
        chain_id = round_id

    # 匿名顺序：与帧号无关的种子洗牌
    items_order = sorted(picked.values(), key=lambda v: v["frame_id"])
    anon_rng = _seed_stream(params["seed"] + ":%d" % round_id, "anon")
    anon_rng.shuffle(items_order)

    now = time.time()
    for idx, v in enumerate(items_order, 1):
        fid = v["frame_id"]
        f = next(x for x in pool if x["id"] == fid)
        ver = db.one("SELECT * FROM frame_versions WHERE frame_id=? AND is_current=1",
                     (fid,))
        locked_path = ver["stored_path"] if ver and ver["stored_path"] else f["stored_path"]
        locked_ver = ver["id"] if ver else None
        locked_name = ver["filename"] if ver else f["filename"]
        locked_rot = int(f["rotation"] or 0)
        try:
            locked_md5 = _md5_file(locked_path) if locked_path and os.path.exists(locked_path) else ""
        except OSError:
            locked_md5 = ""
        tags = sorted(forced_map.get(fid, set()))
        db.run(
            """INSERT INTO review_items(round_id, reel_id, frame_id, anon_index, segment,
                                        selected_by, forced_tags, locked_version_id,
                                        locked_filename, locked_path, locked_rotation,
                                        locked_md5, status, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (round_id, reel_id, fid, idx, v["segment"], v["selected_by"],
             json.dumps(tags, ensure_ascii=False), locked_ver, locked_name,
             locked_path, locked_rot, locked_md5, IT_PENDING, now))
    return round_id


# ---------------------------------------------------------------- 判定

def _round_or_404ish(db, round_id):
    r = db.one("SELECT * FROM review_rounds WHERE id=?", (round_id,))
    if not r:
        raise ValueError("抽查轮次不存在")
    return r


def get_item(db, item_id):
    return db.one("SELECT * FROM review_items WHERE id=?", (item_id,))


def submit_judgement(db, item_id, reviewer, verdict, dims, note, transfer_reshoot):
    """对一张匿名图给出判定，追加审阅历史并更新最新判定；不覆盖历史。

    未结束轮次、复核人一致才能判；fail 必须有备注；verdict=fail 可同时转入重拍。
    返回 (round_row, item_row)。
    """
    it = get_item(db, item_id)
    if not it:
        raise ValueError("抽查项不存在")
    rnd = _round_or_404ish(db, it["round_id"])
    if rnd["status"] != ST_OPEN:
        raise ValueError("该轮次已结束（%s），不能再判；审阅历史保留，重判请另开加抽轮"
                         % STATUS_LABEL.get(rnd["status"], rnd["status"]))
    if it["status"] == IT_VOID:
        raise ValueError("该抽中图像在复核期间已换图，本记录已作废，不计入本轮结论")
    if reviewer.strip() != rnd["reviewer"]:
        raise ValueError("复核人不一致：本轮由 %s 发起" % rnd["reviewer"])
    verdict = verdict if verdict in (IT_PASS, IT_FAIL) else None
    if not verdict:
        raise ValueError("判定结果无效（pass/fail）")

    dim_map = {}
    for k in REVIEW_DIMS:
        dim_map[k] = 1 if (dims or {}).get(k) else 0
    note = (note or "").strip()[:MAX_REMARKS]
    if verdict == IT_FAIL and not note:
        raise ValueError("不合格项必须填写备注（问题说明）")

    now = time.time()
    db.run(
        """INSERT INTO review_judgements(round_id, item_id, reviewer, verdict, dims, note,
                                          transfer_reshoot, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (rnd["id"], item_id, reviewer, verdict,
         json.dumps(dim_map, ensure_ascii=False), note, 1 if transfer_reshoot else 0, now))
    db.run(
        """UPDATE review_items SET status=?, dims=?, note=?, transfer_reshoot=?, decided_at=?
           WHERE id=?""",
        (verdict, json.dumps(dim_map, ensure_ascii=False), note,
         1 if transfer_reshoot else 0, now, item_id))

    # 转入重拍：直接在帧上置重拍标记（后续由补扫流程处理）
    if transfer_reshoot and it["frame_id"]:
        db.run("UPDATE frames SET reshoot=1 WHERE id=?", (it["frame_id"],))
    return rnd, db.one("SELECT * FROM review_items WHERE id=?", (item_id,))


def round_progress(db, round_id):
    """统计轮次进度（作废项不计入）。"""
    rows = db.q(
        """SELECT status, COUNT(*) c FROM review_items WHERE round_id=? GROUP BY status""",
        (round_id,))
    cnt = {IT_PENDING: 0, IT_PASS: 0, IT_FAIL: 0, IT_VOID: 0}
    for r in rows:
        cnt[r["status"]] = r["c"]
    effective = cnt[IT_PASS] + cnt[IT_FAIL]
    return {"pending": cnt[IT_PENDING], "pass": cnt[IT_PASS], "fail": cnt[IT_FAIL],
            "void": cnt[IT_VOID], "total": sum(cnt.values()),
            "effective": effective,
            "all_decided": cnt[IT_PENDING] == 0}


def can_pass(db, round_id):
    """有效项全部判定且失败数不超过门限。"""
    rnd = _round_or_404ish(db, round_id)
    p = round_progress(db, round_id)
    ok = (p["all_decided"] and p["effective"] > 0 and p["fail"] <= rnd["fail_limit"])
    return ok, p


def _versions_still_current(db, round_id):
    """锁定版本是否仍是当前有效版本（MD5 双检）。返回失配项列表。"""
    drift = []
    for it in db.q("SELECT * FROM review_items WHERE round_id=? AND status!=?",
                   (round_id, IT_VOID)):
        if not it["frame_id"]:
            drift.append({"item_id": it["id"], "reason": "帧已不存在"})
            continue
        cur = db.one("SELECT * FROM frame_versions WHERE frame_id=? AND is_current=1",
                     (it["frame_id"],))
        f = db.one("SELECT rotation FROM frames WHERE id=?", (it["frame_id"],))
        if not cur or cur["id"] != it["locked_version_id"]:
            drift.append({"item_id": it["id"], "reason": "图像版本已更换"})
            continue
        if f is not None and int(f["rotation"] or 0) != int(it["locked_rotation"] or 0):
            drift.append({"item_id": it["id"], "reason": "锁定后朝向被调整"})
            continue
        if it["locked_md5"]:
            try:
                path = cur["stored_path"]
                if not path or not os.path.exists(path) or _md5_file(path) != it["locked_md5"]:
                    drift.append({"item_id": it["id"], "reason": "锁定文件内容已变化"})
            except OSError:
                drift.append({"item_id": it["id"], "reason": "锁定文件不可读"})
    return drift


def finish_round(db, round_id, reviewer, action, conclusion=""):
    """结束本轮：pass（通过）/ return（退回整卷）。加抽走 extend_round。

    返回 (status, progress_or_reason)。
    """
    rnd = _round_or_404ish(db, round_id)
    if rnd["status"] != ST_OPEN:
        raise ValueError("轮次已结束，不能重复操作")
    if reviewer.strip() != rnd["reviewer"]:
        raise ValueError("复核人不一致：本轮由 %s 发起" % rnd["reviewer"])
    p = round_progress(db, round_id)
    if action == "pass":
        if not p["all_decided"]:
            raise ValueError("还有 %d 张未判定，不能通过" % p["pending"])
        if p["effective"] == 0:
            raise ValueError("全部抽中项均已作废，不能给出通过结论，请重新抽样")
        if p["fail"] > rnd["fail_limit"]:
            raise ValueError("失败 %d 张超过门限 %d，不能通过；请加抽或退回整卷"
                             % (p["fail"], rnd["fail_limit"]))
        drift = _versions_still_current(db, round_id)
        if drift:
            raise ValueError("抽中后有 %d 张图像被更换/调整，相关记录已作废；"
                             "请重新复核作废项或重新抽样" % len(drift))
        db.run("UPDATE review_rounds SET status=?, conclusion=?, decided_at=? WHERE id=?",
               (ST_PASSED, conclusion or "二次验收通过", time.time(), round_id))
        return ST_PASSED, p
    if action == "return":
        reason = (conclusion or "").strip()
        if not reason:
            raise ValueError("退回整卷必须填写缘由")
        db.run("UPDATE review_rounds SET status=?, conclusion=?, decided_at=? WHERE id=?",
               (ST_RETURNED, "退回整卷：" + reason[:MAX_REMARKS], time.time(), round_id))
        return ST_RETURNED, p
    raise ValueError("未知结论动作")


def extend_round(db, round_id, reviewer, params):
    """未通过轮次发起自动加抽：先把上一轮置 failed，再在同链建新一轮。"""
    rnd = _round_or_404ish(db, round_id)
    if rnd["status"] != ST_OPEN:
        raise ValueError("只有复核中的轮次可以发起加抽")
    if reviewer.strip() != rnd["reviewer"]:
        raise ValueError("复核人不一致：本轮由 %s 发起" % rnd["reviewer"])
    p = round_progress(db, round_id)
    if not p["all_decided"]:
        raise ValueError("还有 %d 张未判定，请判定完本轮再加抽" % p["pending"])
    if p["fail"] <= rnd["fail_limit"]:
        raise ValueError("失败数未超过门限，应直接通过而非加抽")
    db.run("UPDATE review_rounds SET status=?, conclusion=?, decided_at=? WHERE id=?",
           (ST_FAILED, "失败 %d 张超过门限 %d，自动加抽" % (p["fail"], rnd["fail_limit"]),
            time.time(), round_id))
    # 加抽轮沿用同一复核人；校验通过后强制 reviewer 一致
    params = dict(params)
    params["reviewer"] = rnd["reviewer"]
    parent = db.one("SELECT * FROM review_rounds WHERE id=?", (round_id,))
    return create_round(db, rnd["reel_id"], params, parent=parent)


# ---------------------------------------------------------------- 换图作废

def invalidate_frame(db, reel_id, frame_id, reason):
    """某帧当前有效图被更换（重拍接受/回填/拆分/合并）后，作废仍开放轮次中的抽中项。

    已结束轮次的记录一律不动（那是当时的证据）；审阅历史永不删除。
    返回作废的 item_id 列表。
    """
    items = db.q(
        """SELECT ri.* FROM review_items ri JOIN review_rounds rr ON rr.id=ri.round_id
           WHERE ri.frame_id=? AND rr.reel_id=? AND rr.status='open'
             AND ri.status!=?""",
        (frame_id, reel_id, IT_VOID))
    now = time.time()
    ids = []
    for it in items:
        db.run("UPDATE review_items SET status=?, void_reason=? WHERE id=?",
               (IT_VOID, reason[:MAX_REMARKS], it["id"]))
        ids.append(it["id"])
    return ids


def invalidate_rounds_for_frames(db, reel_id, frame_ids, reason):
    """批量作废（边界操作一次影响多帧）。"""
    ids = []
    for fid in dict.fromkeys(frame_ids):
        if fid is None:
            continue
        ids.extend(invalidate_frame(db, reel_id, fid, reason))
    return ids


def _reinstate_item(db, item_id):
    """撤销换图修订时恢复作废项（仅当锁定版本重新成为当前版本）。"""
    it = db.one("SELECT * FROM review_items WHERE id=?", (item_id,))
    if not it or it["status"] != IT_VOID:
        return
    cur = db.one("SELECT id FROM frame_versions WHERE frame_id=? AND is_current=1",
                 (it["frame_id"],)) if it["frame_id"] else None
    if cur and cur["id"] == it["locked_version_id"]:
        # 恢复到作废前的最新判定状态（审阅历史里最后一条）
        last = db.one(
            "SELECT verdict FROM review_judgements WHERE item_id=? ORDER BY id DESC LIMIT 1",
            (item_id,))
        status = last["verdict"] if last else IT_PENDING
        db.run("UPDATE review_items SET status=?, void_reason='' WHERE id=?",
               (status, item_id))


def undo_invalidate(db, item_ids):
    """撤销修订后，把因此作废、且锁定版本已恢复当前的项恢复原状。"""
    for iid in item_ids or []:
        _reinstate_item(db, iid)


# ---------------------------------------------------------------- 有效通过结论

def latest_pass(db, reel_id):
    """返回该卷最新的 passed 轮次；没有则 None。"""
    return db.one("SELECT * FROM review_rounds WHERE reel_id=? AND status='passed' "
                  "ORDER BY id DESC LIMIT 1", (reel_id,))


def valid_pass(db, reel_id):
    """定稿门禁所要求的“有效通过结论”。

    条件：
      1. 存在 passed 轮次，且其后不存在更新的轮次（任何状态）——
         退回/失败/复核中都使旧通过结论失效；
      2. 通过轮中所有未作废项锁定版本仍是当前有效版本（换图即失效）；
      3. 当前没有复核中的轮次。
    """
    rnd = latest_pass(db, reel_id)
    if not rnd:
        return None
    newer = db.one("SELECT id FROM review_rounds WHERE reel_id=? AND id>?",
                   (reel_id, rnd["id"]))
    if newer:
        return None
    if db.one("SELECT id FROM review_rounds WHERE reel_id=? AND status='open'",
              (reel_id,)):
        return None
    drift = _versions_still_current(db, rnd["id"])
    if drift:
        return None
    return rnd


# ---------------------------------------------------------------- 序列化

def _dims(raw):
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def item_anonymous(it):
    """匿名审片视图：不含帧号、文件名、旧告警与处置痕迹（标签也隐藏）。"""
    return {"id": it["id"], "round_id": it["round_id"], "anon_index": it["anon_index"],
            "status": it["status"], "void_reason": it["void_reason"],
            "dims": _dims(it["dims"]), "note": it["note"],
            "transfer_reshoot": it["transfer_reshoot"],
            "decided": it["status"] in (IT_PASS, IT_FAIL)}


def round_detail(db, round_id, include_audit=False):
    rnd = db.one("SELECT * FROM review_rounds WHERE id=?", (round_id,))
    if not rnd:
        return None
    items = db.q("SELECT * FROM review_items WHERE round_id=? ORDER BY anon_index",
                 (round_id,))
    out_items = []
    for it in items:
        d = item_anonymous(it)
        d["segment"] = it["segment"]
        d["selected_by"] = it["selected_by"]
        if include_audit:
            # 审计视图（结束后/管理用）才暴露身份信息
            tags = []
            try:
                tags = json.loads(it["forced_tags"] or "[]")
            except ValueError:
                pass
            fr = db.one("SELECT frame_no, filename FROM frames WHERE id=?",
                        (it["frame_id"],)) if it["frame_id"] else None
            d.update({"frame_id": it["frame_id"],
                      "frame_no": fr["frame_no"] if fr else "（帧已删除）",
                      "filename": fr["filename"] if fr else "",
                      "forced_tags": tags,
                      "forced_labels": [TAG_LABEL.get(t, t) for t in tags],
                      "locked_version_id": it["locked_version_id"],
                      "locked_filename": it["locked_filename"],
                      "locked_md5": it["locked_md5"]})
        out_items.append(d)
    p = round_progress(db, round_id)
    passable, _ = can_pass(db, round_id)
    history = []
    if include_audit:
        for j in db.q(
                """SELECT j.* FROM review_judgements j WHERE j.round_id=?
                   ORDER BY j.id""", (round_id,)):
            history.append({"item_id": j["item_id"], "reviewer": j["reviewer"],
                            "verdict": j["verdict"], "dims": _dims(j["dims"]),
                            "note": j["note"], "transfer_reshoot": j["transfer_reshoot"],
                            "created_at": j["created_at"]})
    return {
        "round": {"id": rnd["id"], "reel_id": rnd["reel_id"],
                  "parent_id": rnd["parent_id"], "chain_id": rnd["chain_id"],
                  "seq": rnd["seq"], "reviewer": rnd["reviewer"], "seed": rnd["seed"],
                  "mode": rnd["mode"], "count": rnd["sample_count"],
                  "ratio": rnd["sample_ratio"], "fail_limit": rnd["fail_limit"],
                  "status": rnd["status"],
                  "status_label": STATUS_LABEL.get(rnd["status"], rnd["status"]),
                  "pool_size": rnd["pool_size"], "n_random": rnd["n_random"],
                  "n_forced": rnd["n_forced"], "n_head": rnd["n_head"],
                  "n_middle": rnd["n_middle"], "n_tail": rnd["n_tail"],
                  "void_reason": rnd["void_reason"], "conclusion": rnd["conclusion"],
                  "created_at": rnd["created_at"], "decided_at": rnd["decided_at"]},
        "items": out_items, "progress": p, "can_pass": passable,
        "history": history,
    }


def rounds_list(db, reel_id):
    out = []
    for r in db.q("SELECT * FROM review_rounds WHERE reel_id=? ORDER BY id", (reel_id,)):
        p = round_progress(db, r["id"])
        out.append({"id": r["id"], "chain_id": r["chain_id"], "seq": r["seq"],
                    "parent_id": r["parent_id"], "reviewer": r["reviewer"],
                    "seed": r["seed"], "status": r["status"],
                    "status_label": STATUS_LABEL.get(r["status"], r["status"]),
                    "mode": r["mode"], "count": r["sample_count"],
                    "ratio": r["sample_ratio"], "fail_limit": r["fail_limit"],
                    "pool_size": r["pool_size"], "n_random": r["n_random"],
                    "n_forced": r["n_forced"], "n_head": r["n_head"],
                    "n_middle": r["n_middle"], "n_tail": r["n_tail"],
                    "conclusion": r["conclusion"],
                    "created_at": r["created_at"], "decided_at": r["decided_at"],
                    "progress": p,
                    "versions_current": (not _versions_still_current(db, r["id"])
                                         if r["status"] == ST_PASSED else None)})
    return out


def review_summary(db, reel_id):
    """主界面 state 用的摘要。"""
    rounds = rounds_list(db, reel_id)
    vp = valid_pass(db, reel_id)
    open_round = db.one("SELECT id FROM review_rounds WHERE reel_id=? AND status='open'",
                        (reel_id,))
    return {
        "rounds": rounds,
        "open_round_id": open_round["id"] if open_round else None,
        "valid_pass_id": vp["id"] if vp else None,
        "default_fail_limit": int(db.get_setting(SETTING_FAIL_LIMIT, DEFAULT_FAIL_LIMIT) or 0),
    }


# ---------------------------------------------------------------- 移交导出

def _round_payload(db, rnd):
    detail = round_detail(db, rnd["id"], include_audit=True)
    items = []
    for it in detail["items"]:
        items.append({
            "anon_index": it["anon_index"], "frame_no": it.get("frame_no"),
            "filename": it.get("filename"), "segment": it["segment"],
            "segment_label": SEG_LABEL.get(it["segment"], it["segment"]),
            "selected_by": it["selected_by"],
            "forced_tags": it.get("forced_tags", []),
            "forced_labels": it.get("forced_labels", []),
            "status": it["status"],
            "dims": {k: it["dims"].get(k, 0) for k in REVIEW_DIMS},
            "note": it["note"], "transfer_reshoot": bool(it["transfer_reshoot"]),
            "void_reason": it["void_reason"],
            "locked_version_id": it.get("locked_version_id"),
            "locked_filename": it.get("locked_filename"),
            "locked_md5": it.get("locked_md5"),
        })
    return {
        "round_id": rnd["id"], "chain_id": rnd["chain_id"], "seq": rnd["seq"],
        "parent_round_id": rnd["parent_id"], "reviewer": rnd["reviewer"],
        "seed": rnd["seed"],
        "scheme": {"mode": rnd["mode"],
                   "count": rnd["sample_count"], "ratio": rnd["sample_ratio"],
                   "fail_limit": rnd["fail_limit"]},
        "sampling": {"pool_size": rnd["pool_size"], "n_random": rnd["n_random"],
                     "n_forced": rnd["n_forced"], "n_head": rnd["n_head"],
                     "n_middle": rnd["n_middle"], "n_tail": rnd["n_tail"]},
        "status": rnd["status"],
        "status_label": STATUS_LABEL.get(rnd["status"], rnd["status"]),
        "conclusion": rnd["conclusion"], "void_reason": rnd["void_reason"],
        "progress": round_progress(db, rnd["id"]),
        "created_at": rnd["created_at"], "decided_at": rnd["decided_at"],
        "items": items,
        "history": detail["history"],
    }


def handoff_payload(db, reel):
    """定稿移交中的二次验收块。必须关联有效通过结论，否则抛 ValueError。"""
    rnd = valid_pass(db, reel["id"])
    if not rnd:
        raise ValueError("没有有效的二次验收通过结论，不能生成移交")
    chain_rounds = db.q("SELECT * FROM review_rounds WHERE chain_id=? ORDER BY seq",
                        (rnd["chain_id"],))
    return {
        "reel_no": reel["reel_no"], "reel_name": reel["name"],
        "final_review": _round_payload(db, rnd),
        "chain": [_round_payload(db, x) for x in chain_rounds],
        "final_disposition": "通过二次验收，准予定稿移交",
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def handoff_csv(db, reel):
    """移交明细 CSV（utf-8-sig）：每张抽中图一行，含方案参数/种子/复核人/作废缘由/处置。"""
    rnd = valid_pass(db, reel["id"])
    if not rnd:
        raise ValueError("没有有效的二次验收通过结论，不能生成移交")
    payload = _round_payload(db, rnd)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["reel_no", "round_id", "chain_id", "轮次序号", "复核人", "种子",
                "抽样方式", "抽取数量", "抽取占比", "失败门限",
                "匿名序号", "帧号", "文件名", "落区", "入选方式", "强制纳入原因",
                "锁定版本id", "锁定文件", "判定", "清晰度", "裁边", "朝向", "污损",
                "内容缺失", "不合格备注", "转入重拍", "作废缘由", "最终处置"])
    mode_label = "数量" if rnd["mode"] == "count" else "占比"

    def verdict_label(it):
        return {"pass": "合格", "fail": "不合格", "void": "已作废",
                "pending": "未判定"}.get(it["status"], it["status"])

    def disposition(it):
        if it["status"] == "void":
            return "作废不计入（" + (it["void_reason"] or "") + "）"
        if it["transfer_reshoot"]:
            return "不合格，已转入重拍"
        if it["status"] == "fail":
            return "不合格（退回/加抽依据）"
        if it["status"] == "pass":
            return "合格，准予移交"
        return ""

    for it in payload["items"]:
        w.writerow([
            reel["reel_no"], rnd["id"], rnd["chain_id"], rnd["seq"], rnd["reviewer"],
            rnd["seed"], mode_label, rnd["sample_count"],
            ("%.4f" % rnd["sample_ratio"]) if rnd["mode"] == "ratio" else "",
            rnd["fail_limit"], it["anon_index"], it["frame_no"], it["filename"],
            it["segment_label"],
            {"random": "随机", "forced": "强制", "both": "随机+强制"}.get(
                it["selected_by"], it["selected_by"]),
            "、".join(it["forced_labels"]),
            it["locked_version_id"], it["locked_filename"], verdict_label(it),
            it["dims"]["clarity"], it["dims"]["crop"], it["dims"]["orientation"],
            it["dims"]["blemish"], it["dims"]["missing"],
            it["note"], "是" if it["transfer_reshoot"] else "",
            it["void_reason"], disposition(it),
        ])
    return buf.getvalue().encode("utf-8-sig")
