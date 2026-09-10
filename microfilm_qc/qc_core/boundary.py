"""帧边界复核核心。

整卷扫描时两个帧可能被粘进同一文件（需要拆分），也可能在画面中间误切（需要合并）。
候选依据：
  - 列（行）亮度谷：帧间压条/接缝表现为亮度极小值
  - 内容投影：列结构（标准差/墨迹）投影在帧间断开
  - 相邻帧号：两帧粘在一起会造成帧号跳号；误切两半常共用/衍生同一帧号
  - 画幅比例：整卷正常画幅的宽高比相对稳定，异常宽/异常窄提示粘连/误切

确认时物理裁切/拼接生成新帧文件，原图与来源区间（frame_sources）永久保留；
全部操作进入可撤销修订（extra.kind='boundary'），撤销时恢复原边界、帧号与位置。

方向约定：
  axis='x' 竖切线（沿列切，画面左右分开），拆出的片段横向拼接可复原 -> layout='h'
  axis='y' 横切线（沿行切，画面上下分开），片段纵向拼接 -> layout='v'
"""
import hashlib
import io
import json
import os
import re
import statistics
import time

from PIL import Image, ImageDraw

from . import imaging

MIN_SEG = 40                 # 片段最小像素宽/高（更小视为零宽片段拦截）
VALLEY_MIN_RUN = 2           # 探测分辨率下的最小谷宽
VALLEY_MAX_FRAC = 0.10       # 谷宽不超过探测轴的 10%
CENTRAL = (0.30, 0.70)       # 主切点只在画面中部 30%-70% 寻找
DETECT_W = 420               # 探测工作分辨率（长边）
CONF_MIN = 0.50              # 候选置信度下限
EDGE_RATIO = 1.35            # 合并接合边尺寸差异上限

# 依据权重（拆分 / 合并）
W_RATIO, W_BRIGHT, W_PROJ, W_NUMBER = 0.22, 0.28, 0.30, 0.20
W_NARROW, W_SEAM, W_FIT, W_DUPNO = 0.30, 0.30, 0.20, 0.20


def _num(frame_no):
    m = re.fullmatch(r"\d+", str(frame_no).strip())
    return int(m.group()) if m else None


def _same_page_number(a_no, b_no):
    """两半页的帧号特征：相同，或同一数字核带字母后缀（17 / 17b、17a / 17b）。"""
    if a_no == b_no:
        return True
    ma = re.fullmatch(r"(\d+)([A-Za-z]+)?", str(a_no))
    mb = re.fullmatch(r"(\d+)([A-Za-z]+)?", str(b_no))
    return bool(ma and mb and ma.group(1) == mb.group(1)
                and (ma.group(2) or mb.group(2)))


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gray(img, long_side=DETECT_W):
    scale = long_side / max(img.width, img.height)
    if scale < 1:
        img = img.resize((max(1, round(img.width * scale)),
                          max(1, round(img.height * scale))), Image.LANCZOS)
    return img.convert("L"), scale


def _profiles(g, axis):
    """沿轴的亮度/结构/墨迹投影。axis='x' 统计每一列（竖切线），'y' 统计每一行（横切线）。"""
    px = g.load()
    W, H = g.size
    n = W if axis == "x" else H
    m = H if axis == "x" else W
    bright, struct, ink = [], [], []
    for t in range(n):
        vals = [px[t, k] if axis == "x" else px[k, t] for k in range(m)]
        mean = sum(vals) / m
        var = sum((v - mean) ** 2 for v in vals) / m
        bright.append(mean / 255.0)
        struct.append((var ** 0.5) / 128.0)
        ink.append(sum(1 for v in vals if v < 128) / m)
    return bright, struct, ink


def _valleys(bright, struct):
    """在结构投影中找“内容断开谷”，并同时记录亮度谷深度。"""
    n = len(struct)
    med_s = statistics.median(struct) or 1e-6
    min_s = max(0.0, min(struct))
    bright_med = statistics.median(bright)
    thr = min_s + (med_s - min_s) * 0.45
    runs, start = [], None
    for i, s in enumerate(list(struct) + [1e9]):
        if s <= thr and i < n:
            if start is None:
                start = i
        elif start is not None:
            if VALLEY_MIN_RUN <= i - start <= n * VALLEY_MAX_FRAC:
                runs.append((start, i))
            start = None
    out = []
    for a, b in runs:
        c = (a + b) // 2
        win = struct[max(0, a - 6):a] + struct[b:min(n, b + 6)]
        base = statistics.median(win) if win else med_s
        depth = max(0.0, min(1.0, (base - min(struct[a:b])) / (base + 1e-6) * 1.4))
        bright_depth = max(0.0, min(1.0, (bright_med - min(bright[a:b])) / 0.6))
        out.append({"pos": c, "depth": round(depth, 3),
                    "bright_depth": round(bright_depth, 3)})
    return sorted(out, key=lambda v: -(v["depth"] + v["bright_depth"]))


def _best_central(valleys, n):
    """粘连接缝应位于画面中部；边缘的谷是页边框/装订线，不作为切点。"""
    lo, hi = int(n * CENTRAL[0]), int(n * CENTRAL[1])
    central = [v for v in valleys if lo <= v["pos"] <= hi]
    return max(central, key=lambda v: v["depth"] + v["bright_depth"], default=None)


def _downsample(vals, k=120):
    if len(vals) <= k:
        return [round(v, 3) for v in vals]
    step = len(vals) / k
    out = []
    for i in range(k):
        seg = vals[int(i * step):int((i + 1) * step) or 1]
        out.append(round(statistics.median(seg), 3))
    return out


def _median_ratio(frames):
    rs = [f["width"] / f["height"] for f in frames
          if not f["placeholder"] and f["width"] and f["height"]]
    return statistics.median(rs) if rs else 0.73


# ---------------------------------------------------------------- 候选检测

def find_split_candidate(frame, frames):
    """分析单张实体帧是否疑似两帧粘连。返回候选 dict 或 None。"""
    if (frame["placeholder"] or not frame["stored_path"]
            or not os.path.exists(frame["stored_path"])):
        return None
    with Image.open(frame["stored_path"]) as im:
        img = imaging.apply_rotation(im.convert("RGB"), frame["rotation"])
        axis = "x" if img.width >= img.height else "y"
        g, scale = _gray(img)
        width, height = img.width, img.height
    bright, struct, ink = _profiles(g, axis)
    valleys = _valleys(bright, struct)
    best = _best_central(valleys, len(struct))
    ratio = width / height
    med = _median_ratio(frames)

    reasons = []
    if axis == "x":
        ratio_ev = max(0.0, min(1.0, (ratio - med) / max(med, 1e-6) * 1.4))
        ratio_txt = "画幅异常宽（宽高比 %.2f，卷中位 %.2f），疑似两帧横向粘连"
    else:
        ratio_ev = max(0.0, min(1.0, (1 / ratio - 1 / med) / max(1 / med, 1e-6) * 1.4))
        ratio_txt = "画幅异常高（高宽比 %.2f，卷中位 %.2f），疑似两帧上下粘连"
    if ratio_ev >= 0.25:
        reasons.append(ratio_txt % (ratio, med))

    bright_ev, proj_ev, num_ev = 0.0, 0.0, 0.0
    cuts = []
    if best:
        pos_full = int(round(best["pos"] / scale))
        cuts = [{"axis": axis, "pos": pos_full,
                 "confidence": round(min(1.0, best["depth"] * 0.6
                                         + best["bright_depth"] * 0.4), 2)}]
        bright_ev = best["bright_depth"]
        if bright_ev >= 0.25:
            side = "列" if axis == "x" else "行"
            coord = "x" if axis == "x" else "y"
            reasons.append("%s亮度谷：%s=%d 处亮度较两侧下降 %.0f%%，疑似帧间压条/接缝"
                           % (side, coord, pos_full, bright_ev * 100))
        proj_ev = best["depth"]
        if proj_ev >= 0.25:
            side = "列" if axis == "x" else "行"
            coord = "x" if axis == "x" else "y"
            reasons.append("内容投影：%s结构投影在 %s=%d 处断开（谷深 %.0f%%），两侧各自成章"
                           % (side, coord, pos_full, proj_ev * 100))

    # 相邻帧号：本帧 n 之后直接是 n+2（或更大），中间帧号疑似被这张粘连图占用
    n = _num(frame["frame_no"])
    later = sorted((f for f in frames if f["position"] > frame["position"]
                    and not f["excluded"]), key=lambda f: f["position"])
    nxt = later[0] if later else None
    if n is not None and nxt is not None:
        n2 = _num(nxt["frame_no"])
        if n2 is not None and n2 - n == 2:
            num_ev = 1.0
            reasons.append("相邻帧号：No.%s 之后直接是 No.%s，缺少 No.%d，可能两帧粘在本图内"
                           % (frame["frame_no"], nxt["frame_no"], n + 1))

    conf = round(min(1.0, W_RATIO * ratio_ev + W_BRIGHT * bright_ev
                     + W_PROJ * proj_ev + W_NUMBER * num_ev), 2)
    if conf < CONF_MIN or not best:
        return None
    return {
        "kind": "split", "frame_id": frame["id"], "frame_no": frame["frame_no"],
        "confidence": conf, "axis": axis, "width": width, "height": height,
        "cuts": cuts, "reasons": reasons,
        "profile": {"axis": axis, "bright": _downsample(bright),
                    "struct": _downsample(struct), "ink": _downsample(ink)},
        "weights": {"ratio": round(W_RATIO * ratio_ev, 3),
                    "bright": round(W_BRIGHT * bright_ev, 3),
                    "proj": round(W_PROJ * proj_ev, 3),
                    "number": round(W_NUMBER * num_ev, 3)},
    }


def _edge_seam(a, b, layout):
    """两片在接合边的内容连续性。layout='v' 比较 a 底边与 b 顶边的列投影；
    'h' 比较 a 右边与 b 左边的行投影。返回 (相关度 0-1, 两边是否都有内容)。"""
    ga, _ = _gray(a, long_side=240)
    gb, _ = _gray(b, long_side=240)
    if layout == "v":
        band = max(3, ga.height // 20)
        w = min(ga.width, gb.width)
        pa = ga.crop((0, ga.height - band, w, ga.height))
        pb = gb.crop((0, 0, w, min(band, gb.height)))
        va = [sum(1 for y in range(pa.height) if pa.getpixel((x, y)) < 128) / pa.height
              for x in range(w)]
        vb = [sum(1 for y in range(pb.height) if pb.getpixel((x, y)) < 128) / pb.height
              for x in range(w)]
    else:
        band = max(3, ga.width // 20)
        h = min(ga.height, gb.height)
        pa = ga.crop((ga.width - band, 0, ga.width, h))
        pb = gb.crop((0, 0, min(band, gb.width), h))
        va = [sum(1 for x in range(pa.width) if pa.getpixel((x, y)) < 128) / pa.width
              for y in range(h)]
        vb = [sum(1 for x in range(pb.width) if pb.getpixel((x, y)) < 128) / pb.width
              for y in range(h)]
    if not va or not vb:
        return 0.0, False
    ma, mb = sum(va) / len(va), sum(vb) / len(vb)
    has_content = ma > 0.02 and mb > 0.02
    cov = sum((x - ma) * (y - mb) for x, y in zip(va, vb))
    da = sum((x - ma) ** 2 for x in va) / len(va)
    dbv = sum((y - mb) ** 2 for y in vb) / len(vb)
    corr = cov / len(va) / ((da * dbv) ** 0.5 + 1e-6)
    return max(0.0, min(1.0, (corr + 1) / 2)), has_content


def find_merge_candidate(a, b, frames):
    """相邻两帧是否疑似同一页被误切为两半。返回候选 dict 或 None。"""
    if (a["placeholder"] or b["placeholder"] or a["excluded"] or b["excluded"]
            or not a["stored_path"] or not b["stored_path"]
            or not os.path.exists(a["stored_path"]) or not os.path.exists(b["stored_path"])):
        return None
    med = _median_ratio(frames)
    with Image.open(a["stored_path"]) as ia, Image.open(b["stored_path"]) as ib:
        ea = imaging.apply_rotation(ia.convert("RGB"), a["rotation"])
        eb = imaging.apply_rotation(ib.convert("RGB"), b["rotation"])
        ra, rb = ea.width / ea.height, eb.width / eb.height
        ha, hb = ea.height / ea.width, eb.height / eb.width  # 高宽比
        med_inv = 1 / med

        tall_narrow = (ra < med * 0.78 and rb < med * 0.78
                       and ea.height >= ea.width and eb.height >= eb.width)
        wide_short = (ha < med_inv * 0.78 and hb < med_inv * 0.78
                      and ea.width >= ea.height and eb.width >= eb.height)
        if tall_narrow:
            axis, layout = "x", "h"
            narrow_ev = max(0.0, min(1.0, (med * 0.78 - max(ra, rb)) / (med * 0.5)))
            shape_txt = "又高又窄"
            ratio_pair = (ra, rb)
        elif wide_short:
            axis, layout = "y", "v"
            narrow_ev = max(0.0, min(1.0, (med_inv * 0.78 - max(ha, hb)) / (med_inv * 0.5)))
            shape_txt = "又宽又矮"
            ratio_pair = (ra, rb)
        else:
            return None
        seam_ev, has_content = _edge_seam(ea, eb, layout)
        if layout == "v":
            fit_ratio = ea.width / (ea.height + eb.height)
        else:
            fit_ratio = (ea.width + eb.width) / max(ea.height, eb.height)

    reasons = []
    if narrow_ev >= 0.2:
        reasons.append("两片画幅都%s（宽高比 %.2f / %.2f，卷中位 %.2f），像同一页被切成两半"
                       % (shape_txt, ratio_pair[0], ratio_pair[1], med))
    if has_content and seam_ev >= 0.62:
        reasons.append("接缝内容连续：No.%s 切边与 No.%s 切边的%s投影相关度 %.0f%%，笔画/行对齐"
                       % (a["frame_no"], b["frame_no"],
                          "列" if layout == "v" else "行", seam_ev * 100))
    elif not has_content or seam_ev < 0.5:
        seam_ev *= 0.5

    fit_ev = max(0.0, 1.0 - abs(fit_ratio - med) / max(med, 1e-6) * 2.2)
    if fit_ev >= 0.4:
        reasons.append("拼合后宽高比 %.2f，回到全卷正常画幅（中位 %.2f）" % (fit_ratio, med))

    dup_ev = 0.0
    if _same_page_number(a["frame_no"], b["frame_no"]):
        dup_ev = 1.0
        reasons.append("相邻帧号：No.%s 与 No.%s 疑似同一页切出的两半（同号/衍生号）"
                       % (a["frame_no"], b["frame_no"]))

    conf = round(min(1.0, W_NARROW * narrow_ev + W_SEAM * seam_ev
                     + W_FIT * fit_ev + W_DUPNO * dup_ev), 2)
    if conf < CONF_MIN:
        return None
    return {
        "kind": "merge", "frame_ids": [a["id"], b["id"]],
        "frame_nos": [a["frame_no"], b["frame_no"]],
        "confidence": conf, "axis": axis, "layout": layout,
        "reasons": reasons,
        "weights": {"narrow": round(W_NARROW * narrow_ev, 3),
                    "seam": round(W_SEAM * seam_ev, 3),
                    "fit": round(W_FIT * fit_ev, 3),
                    "dupno": round(W_DUPNO * dup_ev, 3)},
    }


def candidates(db, reel_id):
    frames = [dict(f) for f in db.frames(reel_id)]
    splits = [c for f in frames for c in [find_split_candidate(f, frames)] if c]
    merges = [c for a, b in zip(frames, frames[1:])
              for c in [find_merge_candidate(a, b, frames)] if c]
    splits.sort(key=lambda c: -c["confidence"])
    merges.sort(key=lambda c: -c["confidence"])
    return {"splits": splits, "merges": merges}


# ---------------------------------------------------------------- 预览（不落库）

def _ordered_cuts(cuts):
    axes = {c["axis"] for c in cuts}
    if not axes:
        raise ValueError("缺少切线")
    if len(axes) > 1:
        raise ValueError("交叉切线：一次拆分只能沿同一方向切线（横切与竖切不能混用）")
    axis = axes.pop()
    poss = sorted(int(c["pos"]) for c in cuts)
    if len(set(poss)) != len(poss):
        raise ValueError("存在重复切点")
    return axis, poss


def _segments(img, axis, poss, min_seg=MIN_SEG):
    """按切点把有效图切成片段，执行零宽/越界校验。"""
    full = img.width if axis == "x" else img.height
    segs, prev = [], 0
    for p in poss + [full]:
        if p <= prev or p > full:
            raise ValueError("切线交叉或越界：切点 %d 未落在区间 (%d,%d] 内" % (p, prev, full))
        if p - prev < min_seg:
            raise ValueError("零宽/过窄片段：%d-%d 仅 %dpx（最小 %dpx），请微调切线"
                             % (prev, p, p - prev, min_seg))
        box = (prev, 0, p, img.height) if axis == "x" else (0, prev, img.width, p)
        segs.append((box, img.crop(box)))
        prev = p
    return segs


def _montage(images, labels, out_w=1100):
    """片段并排预览，加间隔、切线标记与编号。"""
    gap, pad = 18, 10
    label_h = 26
    total = sum(im.width for im in images) + gap * (len(images) - 1)
    scale = min(1.0, out_w / total)
    cells = [im if scale >= 1 else
             im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                       Image.LANCZOS) for im in images]
    W = sum(im.width for im in cells) + gap * (len(cells) - 1) + pad * 2
    H = max(im.height for im in cells) + label_h + pad * 2
    sheet = Image.new("RGB", (W, H), (28, 30, 36))
    d = ImageDraw.Draw(sheet)
    x = pad
    for i, (im, lab) in enumerate(zip(cells, labels)):
        sheet.paste(im, (x, pad))
        d.rectangle([x, pad, x + im.width - 1, pad + im.height - 1],
                    outline=(216, 162, 74), width=2)
        d.text((x + 4, pad + im.height + 5), lab, fill=(230, 230, 230))
        if i < len(cells) - 1:
            cx = x + im.width + gap // 2
            d.line([(cx, pad - 2), (cx, H - pad)], fill=(212, 90, 74), width=2)
        x += im.width + gap
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=84)
    return buf.getvalue()


def _single_jpeg(img, out_w):
    if img.width > out_w:
        img = img.resize((out_w, max(1, round(img.height * out_w / img.width))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=86)
    return buf.getvalue()


def split_preview(frame, cuts, index=None, out_w=1100):
    axis, poss = _ordered_cuts(cuts)
    with Image.open(frame["stored_path"]) as im:
        img = imaging.apply_rotation(im.convert("RGB"), frame["rotation"])
        full = img.width if axis == "x" else img.height
        for p in poss:
            if not (0 < p < full):
                raise ValueError("切点 %d 超出画面（0-%d）" % (p, full))
        segs = _segments(img, axis, poss, min_seg=8)  # 预览放宽，便于拖动过程查看
        if index is not None and 0 <= index < len(segs):
            data = _single_jpeg(segs[index][1].copy(), out_w)
        else:
            labels = []
            for i, (box, _) in enumerate(segs):
                wpx = box[2] - box[0] if axis == "x" else box[3] - box[1]
                labels.append("片段 %d/%d（%dpx）" % (i + 1, len(segs), wpx))
            data = _montage([s[1].copy() for s in segs], labels, out_w)
    return data


def _ordered_merge(frames, frame_ids):
    if len(frame_ids) < 2:
        raise ValueError("请至少选择两帧")
    if len(set(frame_ids)) != len(frame_ids):
        raise ValueError("选择中存在重复帧（重复占用）")
    by_id = {f["id"]: f for f in frames}
    ordered = [by_id.get(i) for i in frame_ids]
    if any(f is None for f in ordered):
        raise ValueError("帧不存在或已被删除")
    by_pos = sorted(frames, key=lambda f: f["position"])
    id_seq = [f["id"] for f in by_pos]
    picked = [id_seq.index(fid) for fid in frame_ids]
    if picked != sorted(picked):
        raise ValueError("请按胶片带顺序选择帧")
    for a, b in zip(ordered, ordered[1:]):
        between = [f for f in frames if a["position"] < f["position"] < b["position"]]
        if between:
            raise ValueError("非相邻合并：No.%s 与 No.%s 之间还隔着 %s"
                             % (a["frame_no"], b["frame_no"],
                                "、".join("No." + g["frame_no"] for g in between)))
    return ordered


def _merge_layout(imgs):
    """推断拼合方向：
    - 两片都“宽而矮”（横切出的上下半页）→ 上下拼接 v；
    - 两片都“高而窄”（竖切出的左右半页）→ 左右拼接 h；
    - 其余（近方形/常规画幅）按接合边较长的一侧拼接。"""
    landscape = sum(1 for im in imgs if im.width >= im.height)
    portrait = len(imgs) - landscape
    if landscape == len(imgs):
        return "v"
    if portrait == len(imgs):
        return "h"
    a = imgs[0]
    return "v" if a.width >= a.height else "h"


def _merge_images(ordered):
    imgs = []
    for f in ordered:
        im = Image.open(f["stored_path"])
        imgs.append(imaging.apply_rotation(im.convert("RGB"), f["rotation"]))
    return imgs


def merge_preview(frames, frame_ids, out_w=1100):
    ordered = _ordered_merge(frames, frame_ids)
    for f in ordered:
        if f["placeholder"]:
            raise ValueError("缺帧占位不能参与合并：No.%s" % f["frame_no"])
    imgs = _merge_images(ordered)
    layout = _merge_layout(imgs)
    out = imaging.stitch(imgs, layout)
    data = _single_jpeg(out, out_w)
    for im in imgs:
        try:
            im.close()
        except Exception:
            pass
    return data


# ---------------------------------------------------------------- 确认拆分 / 合并

def _boundary_dir(root, reel_id):
    d = os.path.join(root, "boundary", str(reel_id))
    os.makedirs(d, exist_ok=True)
    return d


def _renumber_numeric_block(frames, anchor_pos):
    """把包含锚点位置的连续数字帧号块，从块内最小号开始重新编号。
    返回 {frame_id: new_frame_no}（仅变化项）。非数字帧号（如 37A）自然断块。"""
    blocks, cur = [], []
    for f in sorted(frames, key=lambda f: f["position"]):
        if _num(f["frame_no"]) is not None:
            cur.append(f)
        else:
            if cur:
                blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    block = next((b for b in blocks if any(f["position"] == anchor_pos for f in b)), None)
    if not block:
        return {}
    start = min(_num(f["frame_no"]) for f in block)
    changed = {}
    for i, f in enumerate(block):
        new_no = str(start + i)
        if new_no != str(f["frame_no"]):
            changed[f["id"]] = new_no
    return changed


def create_op(db, reel_id, kind, reason):
    """先建操作批次行（detail 占位），确认成功后再回写完整 detail。"""
    return db.add_boundary_op(reel_id, kind, reason, {"created": True})


def discard_op(db, op_id):
    if op_id:
        db.run("DELETE FROM boundary_ops WHERE id=?", (op_id,))


def split_frame(db, root, reel_id, frame_id, cuts, reason="", op_id=None):
    """确认拆分。返回 (anchor_id, [output_ids], extra, op_id)。"""
    f = db.one("SELECT * FROM frames WHERE id=?", (frame_id,))
    if not f:
        raise ValueError("帧不存在")
    if f["reel_id"] != reel_id:
        raise ValueError("跨卷操作：该帧不属于当前卷盘")
    if f["placeholder"]:
        raise ValueError("占位帧没有图像，无法拆分")
    axis, poss = _ordered_cuts(cuts)
    if not poss:
        raise ValueError("请至少添加一条切线")
    if op_id is None:
        op_id = create_op(db, reel_id, "split", reason)

    t0 = time.time()
    paths, boxes, seg_dims = [], [], []
    with Image.open(f["stored_path"]) as im:
        img = imaging.apply_rotation(im.convert("RGB"), f["rotation"])
        full = img.width if axis == "x" else img.height
        for p in poss:
            if not (0 < p < full):
                raise ValueError("切点 %d 超出画面（0-%d）" % (p, full))
        segs = _segments(img, axis, poss)
        bdir = _boundary_dir(root, reel_id)
        for i, (box, segim) in enumerate(segs):
            p = os.path.join(bdir, "op%d_f%d_seg%d.tif" % (op_id, f["id"], i + 1))
            segim.save(p, "TIFF")
            paths.append(p)
            boxes.append(box)
            seg_dims.append((segim.width, segim.height))

    # 第 1 段就地更新锚点帧
    anchor_path = paths[0]
    fp0 = imaging.fingerprint(anchor_path)
    db.run(
        """UPDATE frames SET filename=?, stored_path=?, width=?, height=?, phash=?, cvec=?,
                             brightness=?, ink=?, orient_score=?, rotation=0 WHERE id=?""",
        (os.path.basename(anchor_path), anchor_path, fp0["width"], fp0["height"],
         fp0["phash"], fp0["cvec"], fp0["brightness"], fp0["ink"], fp0["orient_score"],
         frame_id))
    old_ver = db.one("SELECT id FROM frame_versions WHERE frame_id=? AND is_current=1",
                     (frame_id,))
    if old_ver:
        db.run("UPDATE frame_versions SET is_current=0 WHERE id=?", (old_ver["id"],))
    db.add_version(frame_id, reel_id, "boundary", os.path.basename(anchor_path), anchor_path,
                   source="帧边界拆分（第 1/%d 段）" % len(paths), op_id=op_id, is_current=1)

    # 第 2..k 段：先填充锚点之后连续的缺帧占位，再新增帧
    frames_now = db.frames(reel_id)
    anchor_pos = next(x["position"] for x in frames_now if x["id"] == frame_id)
    after = sorted((x for x in frames_now if x["position"] > anchor_pos),
                   key=lambda x: x["position"])
    placeholders = []
    for x in after:
        if x["placeholder"]:
            placeholders.append(x)
        else:
            break

    output_ids = [frame_id]
    new_frame_ids, filled_ids = [], []
    ins_at = anchor_pos
    for k in range(1, len(paths)):
        path = paths[k]
        fp = imaging.fingerprint(path)
        ph = placeholders[k - 1] if k - 1 < len(placeholders) else None
        if ph:
            db.run(
                """UPDATE frames SET filename=?, stored_path=?, width=?, height=?, phash=?, cvec=?,
                                     brightness=?, ink=?, orient_score=?, rotation=0,
                                     placeholder=0, reshoot=0, note=? WHERE id=?""",
                (os.path.basename(path), path, fp["width"], fp["height"], fp["phash"],
                 fp["cvec"], fp["brightness"], fp["ink"], fp["orient_score"],
                 (ph["note"] + " " if ph["note"] else "")
                 + "拆分填充自 No.%s" % f["frame_no"], ph["id"]))
            db.add_version(ph["id"], reel_id, "boundary", os.path.basename(path), path,
                           source="帧边界拆分（第 %d/%d 段，填充缺帧占位）" % (k + 1, len(paths)),
                           op_id=op_id, is_current=1)
            output_ids.append(ph["id"])
            filled_ids.append(ph["id"])
        else:
            ins_at += 1
            db.run("UPDATE frames SET position=position+1 WHERE reel_id=? AND position>=?",
                   (reel_id, ins_at))
            nid = db.add_frame(reel_id, position=ins_at, frame_no="",
                               filename=os.path.basename(path), stored_path=path,
                               note="拆分自 No.%s" % f["frame_no"], placeholder=0, **fp)
            db.add_version(nid, reel_id, "boundary", os.path.basename(path), path,
                           source="帧边界拆分（第 %d/%d 段）" % (k + 1, len(paths)),
                           op_id=op_id, is_current=1)
            output_ids.append(nid)
            new_frame_ids.append(nid)

    _normalize_positions(db, reel_id)
    anchor2 = db.one("SELECT position FROM frames WHERE id=?", (frame_id,))
    changed_no = _renumber_numeric_block(db.frames(reel_id), anchor2["position"])
    for fid, no in changed_no.items():
        db.run("UPDATE frames SET frame_no=? WHERE id=?", (no, fid))

    detail = {
        "axis": axis, "cuts": poss, "full": full,
        "input": [{"frame_id": frame_id, "frame_no": f["frame_no"],
                   "filename": f["filename"], "path": f["stored_path"]}],
        "outputs": [{"frame_id": fid} for fid in output_ids],
        "new_frame_ids": new_frame_ids, "filled_placeholders": filled_ids,
        "renumbered": {str(k): v for k, v in changed_no.items()},
        "segments": [{"dims": list(d)} for d in seg_dims],
        "reason": reason, "op_id": op_id,
    }
    db.run("UPDATE boundary_ops SET detail=? WHERE id=?",
           (json.dumps(detail, ensure_ascii=False), op_id))
    for k, fid in enumerate(output_ids):
        db.add_frame_source(fid, reel_id, "crop", source_frame_id=frame_id, op_id=op_id,
                            source_path=f["stored_path"], source_filename=f["filename"],
                            region={"axis": axis, "box": list(boxes[k]), "cuts": poss,
                                    "index": k, "count": len(output_ids),
                                    "rotation": f["rotation"], "full": full})

    extra = {"boundary": {"op": "split", "op_id": op_id, "anchor_id": frame_id,
                          "new_frame_ids": new_frame_ids, "filled_placeholders": filled_ids,
                          "old_version_id": old_ver["id"] if old_ver else None,
                          "files": paths, "elapsed": round(time.time() - t0, 2)}}
    return frame_id, output_ids, extra, op_id


def _normalize_positions(db, reel_id):
    for i, r in enumerate(db.frames(reel_id)):
        if r["position"] != i:
            db.run("UPDATE frames SET position=? WHERE id=?", (i, r["id"]))


def merge_frames(db, root, reel_id, frame_ids, layout=None, reason="", op_id=None):
    """确认合并连续帧。首帧就地保留为合并结果，其余帧删除（版本行保留，撤销后重新挂回）。"""
    frames = [dict(x) for x in db.frames(reel_id)]
    ordered = _ordered_merge(frames, frame_ids)
    for f in ordered:
        if f["reel_id"] != reel_id:
            raise ValueError("跨卷操作：帧 No.%s 不属于当前卷盘" % f["frame_no"])
        if f["placeholder"]:
            raise ValueError("缺帧占位不能参与合并：No.%s" % f["frame_no"])
    md5s = {}
    for f in ordered:
        m = _md5(f["stored_path"])
        if m in md5s:
            raise ValueError("重复占用：No.%s 与 No.%s 指向同一物理文件（%s），无需合并"
                             % (md5s[m], f["frame_no"], f["filename"]))
        md5s[m] = f["frame_no"]

    imgs = _merge_images(ordered)
    auto = _merge_layout(imgs)
    layout = layout or auto
    if layout not in ("v", "h"):
        raise ValueError("拼接方向只能是 v（上下）或 h（左右）")
    if layout == "v":
        ws = [im.width for im in imgs]
        if max(ws) / max(min(ws), 1) > EDGE_RATIO:
            raise ValueError("接合边对不齐：帧宽差异 %.0f%%（上限 %.0f%%），请人工确认是否同一页"
                             % ((max(ws) / min(ws) - 1) * 100, (EDGE_RATIO - 1) * 100))
    else:
        hs = [im.height for im in imgs]
        if max(hs) / max(min(hs), 1) > EDGE_RATIO:
            raise ValueError("接合边对不齐：帧高差异 %.0f%%（上限 %.0f%%），请人工确认是否同一页"
                             % ((max(hs) / min(hs) - 1) * 100, (EDGE_RATIO - 1) * 100))

    if op_id is None:
        op_id = create_op(db, reel_id, "merge", reason)
    t0 = time.time()
    out = imaging.stitch(imgs, layout)
    anchor = ordered[0]
    out_path = os.path.join(_boundary_dir(root, reel_id),
                            "op%d_f%d_merged.tif" % (op_id, anchor["id"]))
    out.save(out_path, "TIFF")
    for im in imgs:
        try:
            im.close()
        except Exception:
            pass
    fp = imaging.fingerprint(out_path)
    db.run(
        """UPDATE frames SET filename=?, stored_path=?, width=?, height=?, phash=?, cvec=?,
                             brightness=?, ink=?, orient_score=?, rotation=0 WHERE id=?""",
        (os.path.basename(out_path), out_path, fp["width"], fp["height"], fp["phash"],
         fp["cvec"], fp["brightness"], fp["ink"], fp["orient_score"], anchor["id"]))
    old_ver = db.one("SELECT id FROM frame_versions WHERE frame_id=? AND is_current=1",
                     (anchor["id"],))
    if old_ver:
        db.run("UPDATE frame_versions SET is_current=0 WHERE id=?", (old_ver["id"],))
    db.add_version(anchor["id"], reel_id, "boundary", os.path.basename(out_path), out_path,
                   source="帧边界合并（%d 帧%s）" % (
                       len(ordered), "，上下拼接" if layout == "v" else "，左右拼接"),
                   op_id=op_id, is_current=1)

    removed = ordered[1:]
    removed_info = [{"frame_id": f["id"], "frame_no": f["frame_no"],
                     "filename": f["filename"], "path": f["stored_path"]} for f in removed]
    # 注意：不删除 removed 帧的 frame_versions / frame_sources 行——撤销时同 id 帧重新插回，
    # 来源关系必须保持完整（frame_versions.frame_id 无外键约束，悬空期间无害）。
    for f in removed:
        db.run("DELETE FROM frames WHERE id=?", (f["id"],))

    _normalize_positions(db, reel_id)
    anchor2 = db.one("SELECT position FROM frames WHERE id=?", (anchor["id"],))
    changed_no = _renumber_numeric_block(db.frames(reel_id), anchor2["position"])
    for fid, no in changed_no.items():
        db.run("UPDATE frames SET frame_no=? WHERE id=?", (no, fid))

    detail = {
        "layout": layout,
        "input": [{"frame_id": f["id"], "frame_no": f["frame_no"],
                   "filename": f["filename"], "path": f["stored_path"]} for f in ordered],
        "outputs": [{"frame_id": anchor["id"]}],
        "removed": removed_info,
        "renumbered": {str(k): v for k, v in changed_no.items()},
        "dims": [out.width, out.height], "reason": reason, "op_id": op_id,
    }
    db.run("UPDATE boundary_ops SET detail=? WHERE id=?",
           (json.dumps(detail, ensure_ascii=False), op_id))
    db.add_frame_source(anchor["id"], reel_id, "stitch", source_frame_id=anchor["id"],
                        op_id=op_id, source_path=anchor["stored_path"],
                        source_filename=anchor["filename"],
                        region={"layout": layout, "count": len(ordered),
                                "inputs": [f["id"] for f in ordered]})

    extra = {"boundary": {"op": "merge", "op_id": op_id, "anchor_id": anchor["id"],
                          "removed_frame_ids": [f["id"] for f in removed],
                          "old_version_id": old_ver["id"] if old_ver else None,
                          "files": [out_path], "elapsed": round(time.time() - t0, 2)}}
    return anchor["id"], [anchor["id"]], extra, op_id


# ---------------------------------------------------------------- 撤销回滚

def rollback(db, extra):
    """撤销拆分/合并。帧字段（帧号、位置、stored_path 等）由修订快照还原；
    这里处理快照不覆盖的内容：新增帧、版本指针、生成文件、来源记录、操作批次。
    合并删除的帧由快照重新插回（同 id），其版本/来源行在合并时特意保留。"""
    b = extra.get("boundary")
    if not b:
        return
    op_id = b.get("op_id")
    anchor = b["anchor_id"]
    db.run("DELETE FROM frame_versions WHERE frame_id=? AND kind='boundary' AND op_id=?",
           (anchor, op_id))
    if b.get("old_version_id"):
        db.run("UPDATE frame_versions SET is_current=1 WHERE id=?",
               (b["old_version_id"],))
    if b["op"] == "split":
        for nid in b.get("new_frame_ids", []):
            db.run("DELETE FROM frame_sources WHERE frame_id=?", (nid,))
            db.run("DELETE FROM frame_versions WHERE frame_id=?", (nid,))
            db.run("DELETE FROM frames WHERE id=?", (nid,))
        for pid in b.get("filled_placeholders", []):
            db.run("DELETE FROM frame_versions WHERE frame_id=? AND kind='boundary' AND op_id=?",
                   (pid, op_id))
            row = db.one("SELECT id FROM frame_versions WHERE frame_id=? ORDER BY id DESC LIMIT 1",
                         (pid,))
            if row:
                db.run("UPDATE frame_versions SET is_current=1 WHERE id=?", (row["id"],))
    db.run("DELETE FROM frame_sources WHERE op_id=?", (op_id,))
    if op_id:
        db.run("DELETE FROM boundary_ops WHERE id=?", (op_id,))
    for p in b.get("files", []):
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------- 查询 / 导出

def ops_list(db, reel_id):
    out = []
    for r in db.boundary_ops(reel_id):
        d = {"id": r["id"], "kind": r["kind"], "reason": r["reason"],
             "created_at": r["created_at"]}
        try:
            d.update(json.loads(r["detail"] or "{}"))
        except ValueError:
            pass
        out.append(d)
    return out


def changes_json(db, reel_id, reel_no, name):
    frames_by_id = {f["id"]: f for f in db.frames(reel_id)}
    ops = []
    for d in ops_list(db, reel_id):
        outs = []
        for o in d.get("outputs", []):
            fr = frames_by_id.get(o["frame_id"])
            outs.append({"frame_id": o["frame_id"],
                         "frame_no": fr["frame_no"] if fr else None,
                         "filename": fr["filename"] if fr else None})
        ops.append({
            "op_id": d["id"], "type": d["kind"],
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"])),
            "reason": d.get("reason", ""),
            "axis": d.get("axis") or ("y" if d.get("layout") == "v" else "x"),
            "cuts_px": d.get("cuts", []),
            "cuts_frac": [round(c / d.get("full", 1), 4) for c in d.get("cuts", [])],
            "layout": d.get("layout"),
            "inputs": [{"frame_id": s["frame_id"], "frame_no": s["frame_no"],
                        "filename": s["filename"]} for s in d.get("input", [])],
            "removed": [{"frame_id": s["frame_id"], "frame_no": s["frame_no"]}
                        for s in d.get("removed", [])],
            "outputs": outs,
            "renumbered": d.get("renumbered", {}),
            "filled_placeholders": d.get("filled_placeholders", []),
        })
    return {"reel_no": reel_no, "name": name,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "op_count": len(ops), "operations": ops}


def comparison_png(db, op_id, path=None):
    """补扫判断用前后对照图：上排原图（红线标切线/接缝），下排结果帧。"""
    op = db.one("SELECT * FROM boundary_ops WHERE id=?", (op_id,))
    if not op:
        return None
    try:
        detail = json.loads(op["detail"] or "{}")
    except ValueError:
        detail = {}
    cell_h, gap, labels_h = 360, 16, 34

    def fit(im):
        if im.height != cell_h:
            im = im.resize((max(1, round(im.width * cell_h / im.height)), cell_h),
                           Image.LANCZOS)
        return im

    in_imgs, out_imgs = [], []
    for src in detail.get("input", []):
        p = src.get("path")
        if p and os.path.exists(p):
            im = Image.open(p).convert("RGB")
            dr = ImageDraw.Draw(im)
            for c in detail.get("cuts", []):
                if detail.get("axis", "x") == "x":
                    dr.line([(c, 0), (c, im.height)], fill=(220, 60, 50), width=6)
                else:
                    dr.line([(0, c), (im.width, c)], fill=(220, 60, 50), width=6)
            in_imgs.append(fit(im))
    for o in detail.get("outputs", []):
        fr = db.one("SELECT stored_path FROM frames WHERE id=?", (o["frame_id"],))
        if fr and fr["stored_path"] and os.path.exists(fr["stored_path"]):
            out_imgs.append(fit(Image.open(fr["stored_path"]).convert("RGB")))
    if not in_imgs and not out_imgs:
        return None

    def row_w(row):
        return sum(im.width for im in row) + gap * max(0, len(row) - 1)

    W = max([row_w(in_imgs), row_w(out_imgs), 400]) + 32
    H = (cell_h + labels_h) * 2 + gap * 3 + 56
    sheet = Image.new("RGB", (W, H), (24, 26, 31))
    dr = ImageDraw.Draw(sheet)
    dr.text((16, 10),
            "%s修订 #%d ｜ %s" % ("拆分" if op["kind"] == "split" else "合并",
                                 op["id"], op["reason"] or "帧边界修订"),
            fill=(235, 235, 235))
    y = 42
    for label, row in (("修订前（原扫描，红线为切线/接缝）", in_imgs),
                       ("修订后（确认生成帧）", out_imgs)):
        dr.text((16, y), label, fill=(216, 162, 74))
        x, yy = 16, y + labels_h
        for im in row:
            sheet.paste(im, (x, yy))
            dr.rectangle([x, yy, x + im.width - 1, yy + im.height - 1],
                         outline=(90, 90, 100), width=2)
            x += im.width + gap
        y = yy + cell_h + gap
    data = io.BytesIO()
    sheet.save(data, "PNG")
    raw = data.getvalue()
    if path:
        with open(path, "wb") as fh:
            fh.write(raw)
    return raw
