"""质检分析引擎：缺帧 / 重复扫描 / 顺序倒置 / 方向异常 / 亮度突变 / 定稿检查。"""
import re
from .imaging import hamming, cvec_mad

DUP_HAMMING = 3        # 感知哈希距离 <= 此值视为重复扫描
BLANK_INK = 0.02       # 墨迹占比低于此值视为空白引导帧（片头片尾），不参与重复判定
INV_GAIN = 3.0         # 顺序倒置：交换相邻帧后连续性 MAD 收益阈值
INV_RATIO = 1.3        # 顺序倒置：交换前/后 MAD 比值阈值
BRIGHT_JUMP = 45.0     # 相邻帧亮度突变阈值 (0-255)
ORIENT_MIN = 30.0      # 方向分数绝对值下限


def _num(frame_no):
    m = re.search(r"\d+", str(frame_no))
    return int(m.group()) if m else None


def run_checks(db, reel_id):
    """重算连续性告警；已人工确认的告警按签名保留确认状态。"""
    old = db.q("SELECT type, frame_no, message FROM warnings WHERE reel_id=? AND resolved=1", (reel_id,))
    resolved_sigs = {(r["type"], r["frame_no"], r["message"]) for r in old}
    db.run("DELETE FROM warnings WHERE reel_id=?", (reel_id,))

    frames = [dict(r) for r in db.frames(reel_id)]
    active = [f for f in frames if not f["excluded"]]
    found = []

    def add(frame, wtype, message):
        found.append({
            "frame_id": frame["id"] if frame else None,
            "frame_no": frame["frame_no"] if frame else "",
            "type": wtype, "message": message,
        })

    # 1) 缺帧：占位帧始终携带缺帧告警（直至人工确认）；另查帧号序列空洞
    for f in active:
        if f["placeholder"]:
            add(f, "missing",
                "缺帧占位：No.%s 缺少扫描图像（%s），请补扫或标记重拍"
                % (f["frame_no"], f["note"] or "人工插入"))
    nums = [(i, _num(f["frame_no"])) for i, f in enumerate(active)]
    nums = [(i, n) for i, n in nums if n is not None]
    for (i0, n0), (i1, n1) in zip(nums, nums[1:]):
        if n1 - n0 > 1:
            missing = [str(x) for x in range(n0 + 1, n1)]
            label = "、".join(missing[:8]) + ("…" if len(missing) > 8 else "")
            add(active[i1], "missing", "缺帧：%s 与 %s 之间缺少 %s" % (n0, n1, label))

    # 2) 重复扫描：感知哈希近似（空白引导帧除外——片头片尾本就相似）
    hashed = [f for f in active
              if not f["placeholder"] and f["phash"] and f["ink"] >= BLANK_INK]
    hint = {f["id"]: int(f["phash"], 16) for f in hashed}
    for i in range(len(hashed)):
        for j in range(i + 1, len(hashed)):
            a, b = hashed[i], hashed[j]
            d = hamming(hint[a["id"]], hint[b["id"]])
            if d <= DUP_HAMMING:
                add(b, "duplicate",
                    "重复扫描：No.%s 与 No.%s 内容几乎相同（哈希距离 %d），建议剔除其一"
                    % (a["frame_no"], b["frame_no"], d))

    # 4) 方向异常：与全卷方向分数中位数符号相反（先算，供倒置检测排除）
    seq = [f for f in active if not f["placeholder"] and f["cvec"]]
    orient_bad = set()
    scores = sorted(f["orient_score"] for f in seq)
    if scores:
        med = scores[len(scores) // 2]
        if abs(med) >= ORIENT_MIN:
            for f in seq:
                s = f["orient_score"]
                if abs(s) >= ORIENT_MIN and (s < 0) != (med < 0):
                    orient_bad.add(f["id"])
                    add(f, "orientation",
                        "方向异常：No.%s 排版方向与全卷不一致（分数 %.0f，卷中位 %.0f），可能旋转了 90°"
                        % (f["frame_no"], s, med))

    # 3) 顺序倒置：交换相邻两帧能显著降低连续性 MAD（跳过方向异常帧）
    for i in range(len(seq) - 3):
        quad = seq[i:i + 4]
        if any(f["id"] in orient_bad for f in quad):
            continue
        v = [f["cvec"] for f in quad]
        cur = cvec_mad(v[0], v[1]) + cvec_mad(v[2], v[3])
        swapped = cvec_mad(v[0], v[2]) + cvec_mad(v[1], v[3])
        if cur - swapped >= INV_GAIN and cur >= swapped * INV_RATIO:
            f1, f2 = quad[1], quad[2]
            add(f2, "inversion",
                "顺序倒置：No.%s 与 No.%s 疑似颠倒，交换后与前后帧更连贯"
                % (f1["frame_no"], f2["frame_no"]))

    # 5) 相邻帧亮度突变：与最近若干帧的中位亮度比较
    recent = []
    for f in active:
        if f["placeholder"]:
            continue
        if recent:
            base = sorted(recent)[len(recent) // 2]
            delta = f["brightness"] - base
            if abs(delta) >= BRIGHT_JUMP:
                add(f, "brightness",
                    "亮度突变：No.%s 亮度 %.0f，与邻近帧基准 %.0f 相差 %.0f，检查曝光/扫描参数"
                    % (f["frame_no"], f["brightness"], base, abs(delta)))
        recent.append(f["brightness"])
        if len(recent) > 3:
            recent.pop(0)

    for w in found:
        sig = (w["type"], w["frame_no"], w["message"])
        db.run("INSERT INTO warnings(reel_id, frame_id, frame_no, type, message, resolved) VALUES(?,?,?,?,?,?)",
               (reel_id, w["frame_id"], w["frame_no"], w["type"], w["message"],
                1 if sig in resolved_sigs else 0))
    return found


def finalization_checks(db, reel_id):
    """定稿前检查：片头片尾、帧号唯一性、未处理告警、重拍/缺帧统计。"""
    frames = [dict(r) for r in db.frames(reel_id)]
    active = [f for f in frames if not f["excluded"]]
    checks = []

    def endmark(f, kind):
        s = ((f["filename"] or "") + " " + (f["note"] or "")).lower()
        if kind in s:
            return True
        # 兜底：片头片尾通常是高亮度低细节的空白引导段
        return (not f["placeholder"]) and f["brightness"] >= 195

    if active:
        first, last = active[0], active[-1]
        checks.append({"key": "leader", "ok": bool(endmark(first, "leader")),
                       "label": "片头帧",
                       "detail": "首帧 No.%s（%s）" % (first["frame_no"], first["filename"] or "占位")})
        checks.append({"key": "trailer", "ok": bool(endmark(last, "trailer")),
                       "label": "片尾帧",
                       "detail": "末帧 No.%s（%s）" % (last["frame_no"], last["filename"] or "占位")})
    else:
        checks.append({"key": "leader", "ok": False, "label": "片头帧", "detail": "卷内无有效帧"})
        checks.append({"key": "trailer", "ok": False, "label": "片尾帧", "detail": "卷内无有效帧"})

    seen = {}
    for f in active:
        seen[f["frame_no"]] = seen.get(f["frame_no"], 0) + 1
    dups = [k for k, v in seen.items() if v > 1]
    checks.append({"key": "unique", "ok": not dups, "label": "帧号唯一性",
                   "detail": "全部唯一" if not dups else "重复帧号：%s" % "、".join(dups)})

    unresolved = db.one("SELECT COUNT(*) c FROM warnings WHERE reel_id=? AND resolved=0",
                        (reel_id,))["c"]
    checks.append({"key": "warnings", "ok": unresolved == 0, "label": "未处理告警",
                   "detail": "无未处理告警" if unresolved == 0 else "%d 条告警未确认" % unresolved})

    n_reshoot = sum(1 for f in active if f["reshoot"])
    n_missing = sum(1 for f in active if f["placeholder"])
    checks.append({"key": "reshoot", "ok": True, "label": "重拍/缺帧统计",
                   "detail": "标记重拍 %d 帧，缺帧占位 %d 帧（将列入重拍清单）" % (n_reshoot, n_missing)})

    return {"passed": all(c["ok"] for c in checks), "checks": checks}
