"""内置演示卷盘生成器：合成带典型缺陷的样例（缺帧/重复/倒置/旋转/亮度突变）。

页面模型：固定基线版式（页眉/页脚/共有文字行）+ 随帧号单调漂移的内容块与侧标签，
使相邻帧的连续性指纹（MAD）随间隔单调增长——重复、倒置因此可被检出。
"""
import csv
import io
import random
import re
import zipfile
from PIL import Image, ImageDraw, ImageFont

REEL_NO = "R2026-001"
N_BODY = 36                 # 正文帧数（不含片头片尾）
MISSING_NO = 20             # 清单中有、ZIP 中无 -> 缺帧
DUP_SRC, DUP_AT = 12, 13    # No.12 的内容被重复扫描到 No.13 的位置
SWAP_A, SWAP_B = 25, 26     # 内容被扫反的一对
ROTATED_NO = 30             # 旋转 90° 的帧
DARK_NO = 33                # 曝光突变帧
GLUE_NOS = (7, 8)           # 两帧被横向粘进同一文件（No.8 变为缺帧占位，待拆分填充）
MISCUT_NO = 17             # No.17 在画面中间被误切成上下两半（同号衍生帧号 17/17b）
SEAM_INK = 26              # 粘连接缝暗度


def _font(size, bold=True):
    names = ["DejaVuSans-Bold.ttf"] if bold else ["DejaVuSans.ttf"]
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except Exception:
            pass
    return ImageFont.load_default()


def make_page(no, rng, dark=False, rotated=False):
    """合成一页档案：共有版式 + 漂移内容块 + 移动侧标签 + 少量随机行 + 页码。"""
    W, H = 900, 1240
    bg = 96 if dark else 228 + rng.randint(-4, 4)
    img = Image.new("L", (W, H), bg)
    d = ImageDraw.Draw(img)
    ink = 200 if dark else 30
    # 页眉 / 页脚（所有页共有的结构）
    d.rectangle([60, 50, W - 60, 110], outline=ink, width=3)
    d.rectangle([60, H - 100, W - 60, H - 60], outline=ink, width=2)
    # 侧标签：位置随帧号平滑下移（强连续性信号）
    tab_y = 150 + 24 * no
    d.rectangle([W - 170, tab_y, W - 90, tab_y + 120], fill=(170 if dark else 40))
    # 内容块随帧号缓慢漂移
    drift = int(140 + 8 * no)
    d.rectangle([90, drift, W - 90, drift + 240], outline=ink, width=4)
    # 共有基线文字行（同种子 -> 版式一致，模拟同批档案）
    base = random.Random(777)
    y = drift + 280
    while y < H - 160:
        x = 90
        while x < W - 140:
            w = base.randint(40, 150)
            d.rectangle([x, y, x + w, y + 7], fill=(150 if dark else 120))
            x += w + base.randint(18, 50)
        y += base.randint(34, 52)
    # 少量每页随机行（页间差异）
    for _ in range(rng.randint(2, 5)):
        y = rng.randint(drift + 300, H - 200)
        x = rng.randint(90, W - 300)
        d.rectangle([x, y, x + rng.randint(60, 200), y + 7], fill=(150 if dark else 120))
    # 每页随机位置的"印章"块：拉开页间感知哈希距离
    sx = rng.randint(100, W - 260)
    sy = rng.randint(160, H - 260)
    d.rectangle([sx, sy, sx + 140, sy + 70], outline=ink, width=5)
    d.line([sx, sy + 35, sx + 140, sy + 35], fill=ink, width=3)
    d.text((W // 2 - 20, H - 96), str(no), fill=ink, font=_font(48))
    if rotated:
        img = img.rotate(-90, expand=True)
    return img


def make_endmark(kind):
    W, H = 900, 1240
    img = Image.new("L", (W, H), 235)
    d = ImageDraw.Draw(img)
    text = "LEADER" if kind == "leader" else "TRAILER"
    f = _font(90)
    bb = d.textbbox((0, 0), text, font=f)
    d.text(((W - (bb[2] - bb[0])) / 2, H / 2 - 60), text, fill=60, font=f)
    d.rectangle([80, 80, W - 80, H - 80], outline=120, width=6)
    return img


def make_glued(a_img, b_img):
    """两帧横向粘进同一文件（中间留深色压条接缝），模拟整卷扫描粘连。"""
    gap = 46
    h = max(a_img.height, b_img.height)

    def norm(im):
        if im.height != h:
            im = im.resize((round(im.width * h / im.height), h))
        return im

    a_img, b_img = norm(a_img), norm(b_img)
    w = a_img.width + gap + b_img.width
    out = Image.new("L", (w, h), 235)
    out.paste(a_img, (0, 0))
    out.paste(b_img, (a_img.width + gap, 0))
    d = ImageDraw.Draw(out)
    d.rectangle([a_img.width, 0, a_img.width + gap - 1, h], fill=SEAM_INK)
    # 接缝两侧的轻微暗角，强化“压条”观感
    for k in range(10):
        d.line([(a_img.width - 1 - k, 0), (a_img.width - 1 - k, h)], fill=150 + k)
        d.line([(a_img.width + gap + k, 0), (a_img.width + gap + k, h)], fill=150 + k)
    return out


def make_miscut_halves(page_img):
    """同一页在画面中间被误切成上下两半（留出白边），返回 (上半, 下半)。"""
    w, h = page_img.size
    cut = h // 2
    return page_img.crop((0, 0, w, cut)), page_img.crop((0, cut, w, h))


def build_sample():
    """返回 (zip_bytes, manifest_csv_bytes)。"""
    rng = random.Random(20260910)
    body = {no: make_page(no, rng, dark=(no == DARK_NO), rotated=(no == ROTATED_NO))
            for no in range(1, N_BODY + 1)}

    # frame_no -> (filename, image, note)；顺序即胶片带顺序
    entries = [("0", "%s_0000_leader.tif" % REEL_NO, make_endmark("leader"), "leader")]
    for no in range(1, N_BODY + 1):
        if no == MISSING_NO or no == GLUE_NOS[1]:
            continue  # 缺帧 / 粘连图的第二帧：只进清单不进 ZIP
        content_no = {SWAP_A: SWAP_B, SWAP_B: SWAP_A}.get(no, no)  # 顺序倒置
        img = body[content_no]
        if no == DUP_AT:
            img = body[DUP_SRC]  # 重复扫描
        if no == GLUE_NOS[0]:
            img = make_glued(body[GLUE_NOS[0]], body[GLUE_NOS[1]])  # 两帧粘连
            fname = "%s_%04d_glued.tif" % (REEL_NO, no)
            entries.append((str(no), fname, img, "两帧粘连待拆分"))
            continue
        if no == MISCUT_NO:
            # 误切两半：同号衍生帧号，顺序排在 16 与 18 之间
            top, bot = make_miscut_halves(body[no])
            entries.append((str(no), "%s_%04da.jpg" % (REEL_NO, no), top, "误切上半"))
            entries.append(("%db" % no, "%s_%04db.jpg" % (REEL_NO, no), bot, "误切下半"))
            continue
        ext = "jpg" if no % 3 == 0 else "tif"
        entries.append((str(no), "%s_%04d.%s" % (REEL_NO, no, ext), img, ""))
    entries.append((str(N_BODY + 1), "%s_%04d_trailer.tif" % (REEL_NO, N_BODY + 1),
                    make_endmark("trailer"), "trailer"))

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _, fname, img, _ in entries:
            b = io.BytesIO()
            if fname.lower().endswith(".jpg"):
                img.convert("RGB").save(b, "JPEG", quality=90)
            else:
                img.save(b, "TIFF")
            zf.writestr("frames/" + fname, b.getvalue())

    cbuf = io.StringIO()
    w = csv.writer(cbuf)
    w.writerow(["reel_no", "frame_no", "filename", "note"])
    rows = [(fno, fname, note) for fno, fname, _, note in entries]
    present_nos = {r[0] for r in rows}
    if str(MISSING_NO) not in present_nos:  # 清单保留缺帧行（ZIP 中无此文件）
        rows.append((str(MISSING_NO), "%s_%04d.tif" % (REEL_NO, MISSING_NO), ""))
    if str(GLUE_NOS[1]) not in present_nos:  # 粘连第二帧：清单占位，拆分时可就地填充
        rows.append((str(GLUE_NOS[1]), "%s_%04d.tif" % (REEL_NO, GLUE_NOS[1]),
                     "粘连图内第二帧"))

    def sort_key(r):
        m = re.fullmatch(r"(\d+)([A-Za-z]?)", r[0])
        return (int(m.group(1)), m.group(2)) if m else (10 ** 9, r[0])

    for fno, fname, note in sorted(rows, key=sort_key):
        w.writerow([REEL_NO, fno, fname, note])
    return zbuf.getvalue(), cbuf.getvalue().encode("utf-8-sig")
