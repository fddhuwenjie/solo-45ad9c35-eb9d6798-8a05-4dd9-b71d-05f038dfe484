"""图像指纹与缩略图工具（仅依赖 Pillow，全部在本机完成）。"""
import base64
import io
import os
from PIL import Image, ImageDraw, ImageFont

Image.MAX_IMAGE_PIXELS = None  # 档案扫描件可能很大，本机处理自行承担内存

THUMB_CACHE = None  # 由 app.py 注入缓存目录


def _font(size=14):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def dhash(img, size=8):
    """差异感知哈希，返回 int（size*size 位）。对亮度/对比度变化鲁棒。"""
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    w = size + 1
    bits = 0
    for r in range(size):
        base = r * w
        for c in range(size):
            bits = (bits << 1) | (1 if px[base + c] > px[base + c + 1] else 0)
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


CVEC_W, CVEC_H = 36, 48


def cvec(img):
    """连续性向量：36x48 灰度缩略图原始字节（base64）。用于相邻帧版面相似度。"""
    g = img.convert("L").resize((CVEC_W, CVEC_H), Image.LANCZOS)
    return base64.b64encode(g.tobytes()).decode("ascii")


def cvec_mad(b64_a, b64_b):
    """两个连续性向量的平均绝对差（0-255），越小越相似。"""
    a, b = base64.b64decode(b64_a), base64.b64decode(b64_b)
    n = min(len(a), len(b))
    if not n:
        return 255.0
    return sum(abs(a[i] - b[i]) for i in range(n)) / n


def brightness(img):
    """平均亮度 0-255。"""
    g = img.convert("L").resize((64, 64), Image.LANCZOS)
    data = list(g.getdata())
    return sum(data) / len(data)


def ink_ratio(img):
    """墨迹占比（暗像素比例）。接近 0 的为空白/引导帧。"""
    g = img.convert("L").resize((180, 240), Image.LANCZOS)
    data = g.tobytes()
    return sum(1 for b in data if b < 128) / len(data)


def orientation_score(img):
    """方向分数：文本页横向投影方差大。正=横向排版，负=纵向（可能旋转90°）。"""
    g = img.convert("L").resize((96, 128), Image.LANCZOS)
    px = g.load()
    W, H = g.size
    row_means = [sum(px[x, y] for x in range(W)) / W for y in range(H)]
    col_means = [sum(px[x, y] for y in range(H)) / H for x in range(W)]

    def var(v):
        m = sum(v) / len(v)
        return sum((x - m) ** 2 for x in v) / len(v)

    return var(row_means) - var(col_means)


def fingerprint(path, rotation=0):
    """计算一帧的全部指纹。rotation 为当前生效的旋转角度。"""
    with Image.open(path) as img:
        if rotation:
            img = img.rotate(-rotation, expand=True)
        return {
            "width": img.width,
            "height": img.height,
            "phash": format(dhash(img), "016x"),
            "cvec": cvec(img),
            "brightness": round(brightness(img), 2),
            "orient_score": round(orientation_score(img), 1),
            "ink": round(ink_ratio(img), 4),
        }


def apply_rotation(img, rotation):
    if rotation:
        img = img.rotate(-rotation, expand=True)
    return img


def make_thumb(path, rotation, max_w):
    with Image.open(path) as img:
        img = apply_rotation(img.convert("RGB"), rotation)
        ratio = max_w / img.width
        h = max(1, round(img.height * ratio))
        img = img.resize((max_w, h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=82)
        return buf.getvalue()


def placeholder_thumb(frame_no, max_w, label="缺帧"):
    w, h = max_w, round(max_w * 1.35)
    img = Image.new("RGB", (w, h), (245, 242, 235))
    d = ImageDraw.Draw(img)
    m = max(4, max_w // 40)
    # 虚线边框
    dash = max(6, max_w // 24)
    for x in range(m, w - m, dash * 2):
        d.line([(x, m), (min(x + dash, w - m), m)], fill=(150, 60, 60), width=2)
        d.line([(x, h - m), (min(x + dash, w - m), h - m)], fill=(150, 60, 60), width=2)
    for y in range(m, h - m, dash * 2):
        d.line([(m, y), (m, min(y + dash, h - m))], fill=(150, 60, 60), width=2)
        d.line([(w - m, y), (w - m, min(y + dash, h - m))], fill=(150, 60, 60), width=2)
    f1 = _font(max(12, max_w // 8))
    f2 = _font(max(10, max_w // 12))
    t1 = label
    t2 = "No.%s" % frame_no
    for t, f, yy in ((t1, f1, h * 0.42), (t2, f2, h * 0.55)):
        bb = d.textbbox((0, 0), t, font=f)
        d.text(((w - (bb[2] - bb[0])) / 2, yy), t, fill=(150, 60, 60), font=f)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def contact_sheet(frames, thumb_dir, out_path, reel_name):
    """帧总览拼图：frames 为 dict 列表（含 frame_no/placeholder/reshoot/excluded/rotation/stored_path）。"""
    cell_w, cell_h, pad, label_h = 180, 240, 10, 22
    cols = max(1, min(8, len(frames)))
    rows = (len(frames) + cols - 1) // cols
    W = cols * (cell_w + pad) + pad
    H = rows * (cell_h + label_h + pad) + pad + 40
    sheet = Image.new("RGB", (W, H), (30, 30, 34))
    d = ImageDraw.Draw(sheet)
    f_title = _font(20)
    f_lab = _font(13)
    d.text((pad, 8), "卷盘总览 %s  共 %d 帧" % (reel_name, len(frames)), fill=(230, 230, 230), font=f_title)
    for i, fr in enumerate(frames):
        r, c = divmod(i, cols)
        x0 = pad + c * (cell_w + pad)
        y0 = 40 + pad + r * (cell_h + label_h + pad)
        if fr["placeholder"]:
            thumb = Image.open(io.BytesIO(placeholder_thumb(fr["frame_no"], cell_w)))
        else:
            with Image.open(fr["stored_path"]) as im:
                thumb = apply_rotation(im.convert("RGB"), fr["rotation"])
        thumb.thumbnail((cell_w, cell_h), Image.LANCZOS)
        tx = x0 + (cell_w - thumb.width) // 2
        ty = y0 + (cell_h - thumb.height) // 2
        sheet.paste(thumb, (tx, ty))
        border = (90, 90, 96)
        if fr["reshoot"]:
            border = (220, 80, 60)
        elif fr["excluded"]:
            border = (120, 120, 120)
        elif fr["placeholder"]:
            border = (200, 160, 60)
        d.rectangle([x0, y0, x0 + cell_w, y0 + cell_h], outline=border, width=2)
        lab = "No.%s" % fr["frame_no"]
        if fr["reshoot"]:
            lab += " 重拍"
        if fr["excluded"]:
            lab += " 剔除"
        if fr["placeholder"]:
            lab += " 缺失"
        d.text((x0 + 2, y0 + cell_h + 3), lab, fill=(220, 220, 220), font=f_lab)
    sheet.save(out_path, "PNG")
    return out_path
