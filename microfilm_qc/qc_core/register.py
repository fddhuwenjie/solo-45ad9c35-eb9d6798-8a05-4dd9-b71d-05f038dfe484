"""补扫图像配准核对（仅依赖 Pillow，全部本机完成）。

文件名/帧号正确不代表内容属于那一帧：相邻页误装、倒置、裁边过量，仅凭尺寸与亮度
难以发现。本模块在 *有限平移范围* 内、对 *四个旋转方向* 把补扫图与原帧对齐，输出：

  best     最佳变换 {rotation, dx, dy}（工作像素，另给 dx_full/dy_full 原图像素）
  iou      结构相似度：对齐后墨迹掩码的交并比（0-1，越高越像同一页）
  edge_frac 未重合边缘：补扫画幅与原帧画幅未重叠面积占比（0-1，越大越可疑）
  lum_mad  归一化亮度平均绝对差（0-255，曝光差异参考量）

配准搜索用位图大整数打包 + bit_count 移位，单条目四个旋转方向约 0.1s。

核对状态：
  ok           自动配准成功且结构相似度达标、边缘重合正常
  low          配准成功但结构相似度低于阈值（可能误装/裁边过量），需人工核对
  failed       所有方向都配不上（重叠过小/掩码为空）
  no_original  目标是缺帧占位，没有原图可比；必须人工核对后强制接受并填写理由
"""
import io

from PIL import Image, ImageChops, ImageFilter, ImageOps

# ---- 参数 ----
WORK_LONG = 150          # 工作分辨率长边（像素）
SEARCH = 18              # 有限平移范围（工作像素，约原图 12%）
COARSE = 3               # 两级搜索粗步长
TOLERANCE_PX = 1         # 软重合膨胀半径（工作像素）
IOU_PASS = 0.72          # 结构相似度自动通过阈值
EDGE_WARN = 0.12         # 未重合边缘超过此比例提示裁边/画幅不符
EDGE_FAIL = 0.45         # 超过此比例视为配准失败
MIN_INK = 40             # 墨迹像素下限，低于则掩码为空 -> 配准失败

STATUS_OK = "ok"
STATUS_LOW = "low"
STATUS_FAILED = "failed"
STATUS_NO_ORIGINAL = "no_original"

STATUS_LABEL = {
    STATUS_OK: "核对一致",
    STATUS_LOW: "相似度偏低，需人工核对",
    STATUS_FAILED: "配准失败",
    STATUS_NO_ORIGINAL: "无原图可比",
}

ROTATIONS = (0, 90, 180, 270)


# ---------------------------------------------------------------- 预处理

def _prep(img, scale, rotation=0):
    im = img.convert("L")
    if scale < 1:
        im = im.resize((max(1, round(im.width * scale)),
                        max(1, round(im.height * scale))), Image.LANCZOS)
    if rotation:
        im = im.rotate(-rotation, expand=True)
    return im


def ink_mask(im):
    """自适应墨迹掩码（'1' 位图）。亮底取暗像素为墨迹，深底取亮像素；中值去噪。"""
    px = sorted(im.getdata())
    n = len(px)
    if px[n // 2] < 128:
        thr = px[int(n * 0.45)]
        bw = im.point(lambda v: 255 if v > thr + 12 else 0)
    else:
        thr = px[int(n * 0.55)]
        bw = im.point(lambda v: 255 if v < thr - 12 else 0)
    return bw.filter(ImageFilter.MedianFilter(3)).convert("1")


def _pack(mask):
    """'1' 位图打包为大整数；每行按 row_bits=8*ceil(w/8) 对齐，水平移位不跨行缠绕。

    位 (x, y) 对应整数第 y*row_bits + x 位；掩码像素为 1 时该位置位。
    """
    w, h = mask.size
    row_bits = (w + 7) & ~7
    row_bytes = row_bits // 8
    raw = mask.tobytes()
    v = 0
    for y in range(h):
        v |= int.from_bytes(raw[y * row_bytes:(y + 1) * row_bytes], "little") << (y * row_bits)
    return v, w, h, row_bits


def _row_window(stride, x0, y0, x1, y1):
    """参考坐标内矩形 [x0,x1)×[y0,y1) 的位掩码。"""
    v = 0
    row = ((1 << (x1 - x0)) - 1) << x0
    for y in range(y0, y1):
        v |= row << (y * stride)
    return v


# ---------------------------------------------------------------- 配准搜索

def _search_direction(rm, nm):
    """在一个旋转方向上做两级（粗+精）平移搜索，返回 (dx, dy, iou) 或 None。

    ref/new 旋转后可能尺寸/行宽不同，不能直接对不同 stride 的位图做移位与；
    先把两张掩码贴到 *同一宽度* 的公共画布（四周留 SEARCH 余量），再按统一行宽打包。
    平移 (dx,dy) 即 new 相对其名义原点的偏移；new 第 (0,0) 像素对齐参考 (dx,dy)。
    """
    rw0, rh0 = rm.size
    nw0, nh0 = nm.size
    M = SEARCH
    cw = max(rw0, nw0) + 2 * M
    ch = max(rh0, nh0) + 2 * M

    def raster(mask, w, h):
        c = Image.new("1", (cw, ch), 0)
        c.paste(mask, (M, M))
        return c

    rc = raster(rm, rw0, rh0)
    nc = raster(nm, nw0, nh0)
    rv, rw, rh, rs = _pack(rc)
    nv, nw, nh, ns = _pack(nc)
    assert rs == ns
    stride = rs
    rcount = _pack(rm)[0].bit_count()
    ncount = _pack(nm)[0].bit_count()
    if rcount < MIN_INK or ncount < MIN_INK:
        return None

    def eval_at(dx, dy):
        # 画布坐标：ref 原点 (M,M)；new 原点 (M+dx, M+dy)
        nx0, ny0 = M + dx, M + dy
        x0, y0 = max(M, nx0), max(M, ny0)
        x1, y1 = min(M + rw0, nx0 + nw0), min(M + rh0, ny0 + nh0)
        if x1 <= x0 or y1 <= y0:
            return None
        win = _row_window(stride, x0, y0, x1, y1)
        shifted = dy * stride + dx
        sv = (nv << shifted) if shifted >= 0 else (nv >> -shifted)
        sw = sv & win
        inter = (rv & sw).bit_count()
        n_in = sw.bit_count()
        union = rcount + n_in - inter
        return inter / union if union > 0 else 0.0

    best = None
    for dy in range(-SEARCH, SEARCH + 1, COARSE):
        for dx in range(-SEARCH, SEARCH + 1, COARSE):
            jac = eval_at(dx, dy)
            if jac is not None and (best is None or jac > best[2]):
                best = (dx, dy, jac)
    if best is None:
        return None
    bx, by = best[0], best[1]
    fine = best
    for dy in range(by - COARSE, by + COARSE + 1):
        for dx in range(bx - COARSE, bx + COARSE + 1):
            if max(abs(dx), abs(dy)) > SEARCH:
                continue
            jac = eval_at(dx, dy)
            if jac is not None and jac > fine[2]:
                fine = (dx, dy, jac)
    return fine


def _geometry_edge(new0, dx, dy, ref_w, ref_h):
    """画幅未重合比例：new 超出 ref 的面积与 ref 未被覆盖面积的均值，除以 ref 面积。"""
    nx0, ny0 = dx, dy
    nx1, ny1 = nx0 + new0.width, ny0 + new0.height
    inter = (max(0, min(nx1, ref_w) - max(nx0, 0))
             * max(0, min(ny1, ref_h) - max(ny0, 0)))
    new_area = new0.width * new0.height
    ref_area = ref_w * ref_h
    return (max(0, new_area - inter) + ref_area - inter) / 2 / max(1, ref_area)


def _lum_mad(ref_im, new_im, dx, dy):
    """自动对比归一化后的亮度平均绝对差（参考画幅内，覆盖区外按白底 255）。"""
    eq_ref = ImageOps.autocontrast(ref_im, cutoff=1)
    eq_new = ImageOps.autocontrast(new_im, cutoff=1)
    canvas = Image.new("L", (ref_im.width, ref_im.height), 255)
    canvas.paste(eq_new, (dx, dy))
    return sum(ImageChops.difference(eq_ref, canvas).getdata()) / (ref_im.width * ref_im.height)


def register(ref_img, new_img, manual=None):
    """对一对图像做四方向配准。manual=(rotation,dx,dy) 时只在该变换附近精搜（人工微调）。

    返回 dict：rotation/dx/dy（工作像素）/iou/edge_frac/lum_mad/
    ref_wh/new_wh/scale/auto（是否自动搜索）。
    """
    scale = WORK_LONG / max(ref_img.size)
    ref = _prep(ref_img, scale)
    rm = ink_mask(ref)

    if manual is not None:
        rotation = int(manual[0])
        if rotation not in ROTATIONS:
            rotation = 0
        new = _prep(new_img, scale, rotation)
        nm = ink_mask(new)
        hit = _search_direction(rm, nm)
        if hit is None:
            dx, dy, iou = int(manual[1]), int(manual[2]), 0.0
        else:
            dx, dy, iou = hit
        auto = False
    else:
        best = None
        new = None
        for rotation in ROTATIONS:
            cand = _prep(new_img, scale, rotation)
            hit = _search_direction(rm, ink_mask(cand))
            if hit is not None and (best is None or hit[2] > best[1][2]):
                best = (rotation, hit, cand)
        if best is None:
            # 四方向全部配不上：掩码为空或重叠不足
            cand0 = _prep(new_img, scale, 0)
            return {
                "rotation": 0, "dx": 0, "dy": 0, "iou": 0.0,
                "edge_frac": round(_geometry_edge(cand0, 0, 0, ref.width, ref.height), 4),
                "lum_mad": round(_lum_mad(ref, cand0, 0, 0), 2),
                "ref_wh": [ref.width, ref.height], "new_wh": [cand0.width, cand0.height],
                "scale": scale, "auto": True, "ok": False,
            }
        rotation, (dx, dy, iou), new = best
        auto = True

    edge_frac = _geometry_edge(new, dx, dy, ref.width, ref.height)
    lum = _lum_mad(ref, new, dx, dy)
    return {
        "rotation": rotation, "dx": int(dx), "dy": int(dy), "iou": round(iou, 4),
        "edge_frac": round(edge_frac, 4), "lum_mad": round(lum, 2),
        "ref_wh": [ref.width, ref.height], "new_wh": [new.width, new.height],
        "scale": scale, "auto": auto,
        "ok": iou >= IOU_PASS and edge_frac <= EDGE_WARN,
    }


def classify(reg):
    """根据配准结果给出核对状态。"""
    if not reg or reg.get("iou", 0) <= 0:
        return STATUS_FAILED
    if reg["edge_frac"] > EDGE_FAIL:
        return STATUS_FAILED
    if reg["iou"] < IOU_PASS or reg["edge_frac"] > EDGE_WARN:
        return STATUS_LOW
    return STATUS_OK


# ---------------------------------------------------------------- 叠加 / 差异图

def overlay_jpeg(ref_img, new_img, reg, out_w=900, blend=0.5):
    """按最佳变换把补扫图叠到原帧上（原帧红、补扫青），输出 JPEG。blend=0 只看原图。"""
    return _composite(ref_img, new_img, reg, out_w, mode="overlay", blend=blend)


def diff_jpeg(ref_img, new_img, reg, out_w=900):
    """差异图：对齐后两帧亮度差放大显示（亮处=内容不一致）。"""
    return _composite(ref_img, new_img, reg, out_w, mode="diff")


def aligned_new(ref_img, new_img, reg, out_w=900):
    """仅把按变换对齐后的补扫图渲染到原帧画幅（供闪烁切换）。"""
    return _composite(ref_img, new_img, reg, out_w, mode="new")


def _aligned_pair(ref_img, new_img, reg):
    """在原帧画幅坐标系渲染灰度原图与对齐后的补扫图（同尺寸 L）。"""
    rot = int((reg or {}).get("rotation", 0))
    dx_full = int(round(reg.get("dx_full", reg.get("dx", 0))))
    dy_full = int(round(reg.get("dy_full", reg.get("dy", 0))))
    ref = ref_img.convert("L")
    new = new_img.convert("L")
    if rot:
        new = new.rotate(-rot, expand=True)
    canvas = Image.new("L", ref.size, 255)
    canvas.paste(new, (dx_full, dy_full))
    return ref, canvas


def _composite(ref_img, new_img, reg, out_w, mode="overlay", blend=0.5):
    ref, new = _aligned_pair(ref_img, new_img, reg)
    if mode == "new":
        out = new
    elif mode == "diff":
        d = ImageChops.difference(ImageOps.autocontrast(ref, cutoff=1),
                                  ImageOps.autocontrast(new, cutoff=1))
        out = ImageOps.autocontrast(d, cutoff=0).convert("RGB")
    else:
        # 叠加图：原帧墨跓染红通道，补扫墨跓染绿+蓝通道。
        # 两边都有墨迹（重合）-> 近白；仅原帧有 -> 红；仅补扫有 -> 青；都无 -> 黑。
        r = ImageOps.autocontrast(ref, cutoff=1)
        c = ImageOps.autocontrast(new, cutoff=1)
        zero = Image.new("L", r.size, 0)
        check = Image.merge("RGB", (r, c, c))
        # 轻度放大非重合色差，重合处保持高亮出页面结构
        over = Image.blend(Image.merge("RGB", (r, r, r)), check,
                           max(0.0, min(1.0, blend)))
        out = over
    if out.width > out_w:
        out = out.resize((out_w, max(1, round(out.height * out_w / out.width))), Image.LANCZOS)
    buf = io.BytesIO()
    out.convert("RGB").save(buf, "JPEG", quality=84)
    return buf.getvalue()


def full_translation(reg):
    """把工作像素平移换算为原图像素（供存储与前端按原始坐标理解）。"""
    scale = reg.get("scale", 1.0) or 1.0
    reg = dict(reg)
    reg["dx_full"] = int(round(reg["dx"] / scale))
    reg["dy_full"] = int(round(reg["dy"] / scale))
    return reg
