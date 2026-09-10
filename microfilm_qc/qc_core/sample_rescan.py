"""内置演示补扫批次生成器。

先在演示卷上准备回填目标（标记重拍、补插占位），再合成一批补扫 ZIP + 回填清单，
内含改名回填、重拍替换，以及跨卷、一个原帧多文件、同文件重复占用、目标不存在、
ZIP 缺文件、清单外文件等拦截样例。
"""
import csv
import io
import random
import zipfile

from . import analysis, sample_reel

EXTRA_PLACEHOLDER_NO = "37A"   # 为演示“一帧多文件”临时插入的占位帧（非数字帧号，避免序列空洞告警）


def _prepare_reel(db, reel_id):
    """标记重拍帧并插入额外占位（作为一个可撤销修订）。"""
    db.save_revision(reel_id, "生成演示补扫：标记 No.12/No.13/No.33 重拍、插入 No.%s 占位"
                     % EXTRA_PLACEHOLDER_NO)
    for no in ("12", "13", "33"):
        db.run("UPDATE frames SET reshoot=1 WHERE reel_id=? AND frame_no=?",
               (reel_id, no))
    frames = db.frames(reel_id)
    trailer = next((f for f in frames
                    if (f["note"] or "").lower() == "trailer"
                    or "trailer" in (f["filename"] or "").lower()), frames[-1])
    pos = trailer["position"]
    db.run("UPDATE frames SET position=position+1 WHERE reel_id=? AND position>=?",
           (reel_id, pos))
    db.add_frame(reel_id, position=pos, frame_no=str(EXTRA_PLACEHOLDER_NO),
                 filename="%s_%s.tif" % (sample_reel.REEL_NO, "0037A"),
                 note="补扫演示占位", placeholder=1)
    analysis.run_checks(db, reel_id)


def _page_bytes(img, fmt, jpg=True):
    b = io.BytesIO()
    if jpg:
        img.convert("RGB").save(b, "JPEG", quality=90)
    else:
        img.save(b, "TIFF")
    return b.getvalue()


def build_sample_rescan(db, reel_id, reel_no):
    """返回 (zip_bytes, manifest_csv_bytes)。"""
    _prepare_reel(db, reel_id)

    # 复现与原卷同分布的页面（同种子、同顺序）
    rng = random.Random(20260910)
    pages = {no: sample_reel.make_page(no, rng) for no in range(1, sample_reel.N_BODY + 2)}

    zfiles = {}  # zip 内路径 -> bytes
    def put(fname, data):
        zfiles["rescan/" + fname] = data

    # 正常待回填：改名文件 / 重拍替换 / 重复扫重拍 / 新占位
    renamed = "%s_%04d_renamed_retake.jpg" % ("RESCAN", 20)
    put(renamed, _page_bytes(pages[20], "jpg"))
    f33 = "%s_%04d_reshoot.tif" % (reel_no, 33)
    put(f33, _page_bytes(pages[33], "tif", jpg=False))
    f13 = "%s_%04d_fixed.tif" % (reel_no, 13)
    put(f13, _page_bytes(pages[13], "tif", jpg=False))
    f37 = "%s_%04d.tif" % (reel_no, 37)
    put(f37, _page_bytes(pages[37], "tif", jpg=False))

    # 拦截：跨卷（文件存在，但清单卷号不符）
    f_wrong = "%s_%04d_wrongreel.jpg" % (reel_no, 5)
    put(f_wrong, _page_bytes(pages[5], "jpg"))

    # 拦截：目标不存在（文件存在）
    f_missing_target = "%s_%04d.tif" % (reel_no, 77)
    put(f_missing_target, _page_bytes(pages[36], "tif", jpg=False))

    # 拦截：同一文件重复占用 —— 直接取卷内 No.12 当前有效文件的字节（MD5 相同）
    f12 = db.one("SELECT * FROM frames WHERE reel_id=? AND frame_no='12'", (reel_id,))
    f_dup = "%s_%04d_dup.tif" % (reel_no, 12)
    with open(f12["stored_path"], "rb") as fh:
        put(f_dup, fh.read())

    # 拦截：一个原帧对应多个文件 —— 同帧号再来一份不同内容
    f37_extra = "%s_%04d_extra.tif" % (reel_no, 37)
    alt_rng = random.Random(555)
    put(f37_extra, _page_bytes(sample_reel.make_page(37, alt_rng), "tif", jpg=False))

    # 拦截：清单有、ZIP 中无
    f_notin_zip = "%s_%04d_missing.tif" % (reel_no, 99)

    # 清单外文件（仅进 ZIP，不进清单）
    put("%s_9999_orphan.jpg" % reel_no, _page_bytes(pages[10], "jpg"))

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in zfiles.items():
            zf.writestr(path, data)

    rows = [
        (reel_no, "20", renamed, "缺帧补扫（文件已改名）"),
        (reel_no, "33", f33, "亮度异常重拍"),
        (reel_no, "13", f13, "重复扫描重拍"),
        (reel_no, str(EXTRA_PLACEHOLDER_NO), f37, "新增占位回填"),
        ("R999-888", "5", f_wrong, "跨卷误提交"),
        (reel_no, "77", f_missing_target, "错卷/帧号有误"),
        (reel_no, "12", f_dup, "重复提交同一文件"),
        (reel_no, str(EXTRA_PLACEHOLDER_NO), f37_extra, "重复指向同一原帧"),
        (reel_no, "99", f_notin_zip, "ZIP 中漏放"),
    ]
    cbuf = io.StringIO()
    w = csv.writer(cbuf)
    w.writerow(["reel_no", "frame_no", "filename", "note"])
    for r in rows:
        w.writerow(r)
    return zbuf.getvalue(), cbuf.getvalue().encode("utf-8-sig")
