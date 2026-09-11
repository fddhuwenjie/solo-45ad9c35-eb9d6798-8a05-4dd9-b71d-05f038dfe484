"""SQLite 数据层：卷盘、帧、告警、可撤销修订、补扫批次与帧版本。"""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS reels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    reel_no TEXT NOT NULL,
    created_at REAL NOT NULL,
    finalized INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    frame_no TEXT NOT NULL,
    filename TEXT DEFAULT '',
    stored_path TEXT DEFAULT '',
    note TEXT DEFAULT '',
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    phash TEXT DEFAULT '',
    cvec TEXT DEFAULT '',
    brightness REAL DEFAULT 0,
    ink REAL DEFAULT 0,
    orient_score REAL DEFAULT 0,
    rotation INTEGER NOT NULL DEFAULT 0,
    excluded INTEGER NOT NULL DEFAULT 0,
    placeholder INTEGER NOT NULL DEFAULT 0,
    reshoot INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
    frame_id INTEGER,
    frame_no TEXT DEFAULT '',
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    extra TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rescan_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    note TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rescan_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES rescan_batches(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    md5 TEXT DEFAULT '',
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    phash TEXT DEFAULT '',
    cvec TEXT DEFAULT '',
    brightness REAL DEFAULT 0,
    ink REAL DEFAULT 0,
    orient_score REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rescan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES rescan_batches(id) ON DELETE CASCADE,
    file_id INTEGER REFERENCES rescan_files(id) ON DELETE SET NULL,
    reel_no TEXT DEFAULT '',
    frame_no TEXT NOT NULL DEFAULT '',
    filename TEXT DEFAULT '',
    target_frame_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    block_reason TEXT DEFAULT '',
    decision_note TEXT DEFAULT '',
    decided_at REAL DEFAULT 0,
    seq INTEGER NOT NULL DEFAULT 0,
    reg_status TEXT DEFAULT '',          -- 配准核对：ok/low/failed/no_original（''=尚未核对）
    reg_detail TEXT DEFAULT '',          -- JSON：最佳变换、各项指标、自动/人工
    reg_manual INTEGER NOT NULL DEFAULT 0,
    force_reason TEXT DEFAULT ''         -- 单项强制接受理由（低置信/失败/无原图时必填）
);
CREATE TABLE IF NOT EXISTS frame_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_id INTEGER NOT NULL,
    reel_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT DEFAULT '',
    stored_path TEXT DEFAULT '',
    is_current INTEGER NOT NULL DEFAULT 1,
    source TEXT DEFAULT '',
    batch_id INTEGER,
    item_id INTEGER,
    op_id INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frames_reel ON frames(reel_id, position);
CREATE INDEX IF NOT EXISTS idx_warn_reel ON warnings(reel_id);
CREATE INDEX IF NOT EXISTS idx_rfiles_batch ON rescan_files(batch_id);
CREATE INDEX IF NOT EXISTS idx_ritems_batch ON rescan_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_fver_frame ON frame_versions(frame_id, is_current);
CREATE INDEX IF NOT EXISTS idx_fver_reel ON frame_versions(reel_id);

-- 帧边界复核：每次“确认拆分/合并”为一个操作批次
CREATE TABLE IF NOT EXISTS boundary_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'split',   -- split / merge
    reason TEXT DEFAULT '',
    detail TEXT DEFAULT '',               -- JSON：输入帧、切线、输出帧、依据
    created_at REAL NOT NULL
);
-- 输出来源关系：每张当前帧来自哪个原图（可链多代）、原图区间
CREATE TABLE IF NOT EXISTS frame_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_id INTEGER NOT NULL,
    reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
    source_frame_id INTEGER,
    op_id INTEGER REFERENCES boundary_ops(id) ON DELETE SET NULL,
    source_path TEXT DEFAULT '',          -- 原始扫描文件（永不删除）
    source_filename TEXT DEFAULT '',
    region TEXT DEFAULT '',               -- JSON: {x0,y0,x1,y1,w,h} 或拼接描述
    kind TEXT NOT NULL DEFAULT 'crop',    -- crop / stitch / whole
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bops_reel ON boundary_ops(reel_id, id);
CREATE INDEX IF NOT EXISTS idx_fsrc_frame ON frame_sources(frame_id);
CREATE INDEX IF NOT EXISTS idx_fsrc_reel ON frame_sources(reel_id);

-- 二次验收：一轮可复现抽样 + 匿名逐张复核。轮次链（parent_id）记录“加抽”关系。
CREATE TABLE IF NOT EXISTS review_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES review_rounds(id) ON DELETE SET NULL,
    chain_id INTEGER NOT NULL,               -- 同一链（首抽/各次加抽共享）
    seq INTEGER NOT NULL DEFAULT 1,          -- 链内序号：1=首抽，2..=第 n 次加抽
    reviewer TEXT NOT NULL DEFAULT '',
    seed TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'count',      -- count / ratio
    sample_count INTEGER NOT NULL DEFAULT 0,-- 随机抽取目标数（不含强制加入项）
    sample_ratio REAL NOT NULL DEFAULT 0,    -- mode=ratio 时的占比（0-1）
    fail_limit INTEGER NOT NULL DEFAULT 0,   -- 可配置失败门限（失败数 <= 门限方可通过）
    status TEXT NOT NULL DEFAULT 'open',     -- open / passed / failed / returned
    pool_size INTEGER NOT NULL DEFAULT 0,    -- 抽样总体（非占位、非剔除）
    n_random INTEGER NOT NULL DEFAULT 0,     -- 随机实际命中数
    n_forced INTEGER NOT NULL DEFAULT 0,     -- 强制加入项数（去重后）
    n_head INTEGER NOT NULL DEFAULT 0,
    n_middle INTEGER NOT NULL DEFAULT 0,
    n_tail INTEGER NOT NULL DEFAULT 0,
    void_reason TEXT DEFAULT '',             -- 整轮作废原因（换图等）；结论与审阅历史保留
    conclusion TEXT DEFAULT '',              -- 结束结论说明（通过/加抽/退回时写入）
    created_at REAL NOT NULL,
    decided_at REAL NOT NULL DEFAULT 0
);
-- 抽中的图像项：版本在抽样时锁定（locked_version_id / path / rotation / md5）。
-- status: pending/pass/fail/void；审阅历史存 review_judgements，行本身只反映最新判定。
CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES review_rounds(id) ON DELETE CASCADE,
    reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
    frame_id INTEGER,                        -- 帧被删除（合并撤销前悬空期）时可空
    anon_index INTEGER NOT NULL,             -- 匿名审片顺序（种子洗牌，与帧号无关）
    segment TEXT NOT NULL DEFAULT '',        -- head / middle / tail / forced
    selected_by TEXT NOT NULL DEFAULT '',    -- random / forced / both
    forced_tags TEXT DEFAULT '',             -- 强制加入原因标签 JSON：reshoot/fill/split/reg_force...
    locked_version_id INTEGER,
    locked_filename TEXT DEFAULT '',
    locked_path TEXT DEFAULT '',
    locked_rotation INTEGER NOT NULL DEFAULT 0,
    locked_md5 TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / pass / fail / void
    dims TEXT DEFAULT '',                    -- JSON：五项判定 clarity/crop/orient/blemish/missing
    note TEXT DEFAULT '',                    -- 不合格备注（fail 必填）
    transfer_reshoot INTEGER NOT NULL DEFAULT 0,  -- 判定同时转入重拍
    void_reason TEXT DEFAULT '',
    superseded_by INTEGER,                  -- 换图/朝向调整后重判：指向替代它的新 review_items.id（旧记录保持 void）
    decided_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
-- 审阅历史：只增不改不覆盖（同一项可留下多条，作废后重判另开轮次）。
CREATE TABLE IF NOT EXISTS review_judgements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES review_rounds(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES review_items(id) ON DELETE CASCADE,
    reviewer TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL DEFAULT '',        -- pass / fail
    dims TEXT DEFAULT '',
    note TEXT DEFAULT '',
    transfer_reshoot INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS qc_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rrounds_reel ON review_rounds(reel_id, id);
CREATE INDEX IF NOT EXISTS idx_ritems_round ON review_items(round_id, anon_index);
CREATE INDEX IF NOT EXISTS idx_ritems_frame ON review_items(frame_id);
CREATE INDEX IF NOT EXISTS idx_rjudge_item ON review_judgements(item_id, id);
"""

FRAME_COLS = ["id", "position", "frame_no", "filename", "stored_path", "note",
              "width", "height", "phash", "cvec", "brightness", "ink", "orient_score",
              "rotation", "excluded", "placeholder", "reshoot"]


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """旧库补列。"""
        cols = {r["name"] for r in self.q("PRAGMA table_info(revisions)")}
        if "extra" not in cols:
            self.conn.execute("ALTER TABLE revisions ADD COLUMN extra TEXT DEFAULT ''")
        fv_cols = {r["name"] for r in self.q("PRAGMA table_info(frame_versions)")}
        if "op_id" not in fv_cols:
            self.conn.execute("ALTER TABLE frame_versions ADD COLUMN op_id INTEGER")
        ri_cols = {r["name"] for r in self.q("PRAGMA table_info(rescan_items)")}
        if "reg_status" not in ri_cols:
            self.conn.execute("ALTER TABLE rescan_items ADD COLUMN reg_status TEXT DEFAULT ''")
        if "reg_detail" not in ri_cols:
            self.conn.execute("ALTER TABLE rescan_items ADD COLUMN reg_detail TEXT DEFAULT ''")
        if "reg_manual" not in ri_cols:
            self.conn.execute("ALTER TABLE rescan_items ADD COLUMN reg_manual INTEGER NOT NULL DEFAULT 0")
        if "force_reason" not in ri_cols:
            self.conn.execute("ALTER TABLE rescan_items ADD COLUMN force_reason TEXT DEFAULT ''")
        rvi_cols = {r["name"] for r in self.q("PRAGMA table_info(review_items)")}
        if "superseded_by" not in rvi_cols:
            self.conn.execute(
                "ALTER TABLE review_items ADD COLUMN superseded_by INTEGER")

    def q(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def run(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    # ---- 帧 ----
    def frames(self, reel_id, include_excluded=True):
        sql = "SELECT * FROM frames WHERE reel_id=?"
        if not include_excluded:
            sql += " AND excluded=0"
        return self.q(sql + " ORDER BY position", (reel_id,))

    def add_frame(self, reel_id, **kw):
        cols = ["reel_id"] + [c for c in FRAME_COLS[1:] if c in kw]
        vals = [reel_id] + [kw[c] for c in cols[1:]]
        cur = self.run("INSERT INTO frames(%s) VALUES(%s)" %
                       (",".join(cols), ",".join("?" * len(cols))), vals)
        return cur.lastrowid

    # ---- 帧版本（来源关系） ----
    def add_version(self, frame_id, reel_id, kind, filename, stored_path,
                    source="", batch_id=None, item_id=None, is_current=1, op_id=None):
        cur = self.run(
            """INSERT INTO frame_versions(frame_id, reel_id, kind, filename, stored_path,
                                          is_current, source, batch_id, item_id, op_id, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (frame_id, reel_id, kind, filename, stored_path, is_current, source,
             batch_id, item_id, op_id, time.time()))
        return cur.lastrowid

    def current_version(self, frame_id):
        return self.one("SELECT * FROM frame_versions WHERE frame_id=? AND is_current=1",
                        (frame_id,))

    # ---- 修订（撤销） ----
    def snapshot(self, reel_id):
        rows = self.frames(reel_id)
        return [{c: r[c] for c in FRAME_COLS} for r in rows]

    def save_revision(self, reel_id, action, extra=None):
        snap = json.dumps(self.snapshot(reel_id), ensure_ascii=False)
        self.run(
            "INSERT INTO revisions(reel_id, action, snapshot, extra, created_at) VALUES(?,?,?,?,?)",
            (reel_id, action, snap,
             json.dumps(extra, ensure_ascii=False) if extra else "", time.time()))

    def undo(self, reel_id):
        """还原最近一次修订。帧按 id 原地更新/插入/删除，保证版本表等引用不失效。

        返回 (action, extra_dict_or_None)。
        """
        rev = self.one("SELECT * FROM revisions WHERE reel_id=? ORDER BY id DESC", (reel_id,))
        if not rev:
            return None
        snap = json.loads(rev["snapshot"])
        snap_ids = {fr["id"] for fr in snap}
        for r in self.frames(reel_id):
            if r["id"] not in snap_ids:
                self.run("DELETE FROM frame_versions WHERE frame_id=?", (r["id"],))
                self.run("DELETE FROM frames WHERE id=?", (r["id"],))
        for fr in snap:
            exists = self.one("SELECT 1 FROM frames WHERE id=?", (fr["id"],))
            if exists:
                sets = ",".join("%s=?" % c for c in FRAME_COLS[1:])
                self.run("UPDATE frames SET %s WHERE id=?" % sets,
                         [fr[c] for c in FRAME_COLS[1:]] + [fr["id"]])
            else:
                ph = ",".join(["?"] * (len(FRAME_COLS) + 1))
                self.run("INSERT INTO frames(reel_id,%s) VALUES(%s)" %
                         (",".join(FRAME_COLS), ph),
                         [reel_id] + [fr[c] for c in FRAME_COLS])
        self.run("DELETE FROM revisions WHERE id=?", (rev["id"],))
        self.conn.commit()
        extra = json.loads(rev["extra"]) if rev["extra"] else None
        return {"action": rev["action"], "extra": extra}

    def revisions(self, reel_id):
        return self.q("SELECT id, action, created_at FROM revisions WHERE reel_id=? ORDER BY id DESC",
                      (reel_id,))

    def restore_frames_snapshot(self, reel_id, snapshot):
        """把帧表恢复到给定快照（不依赖 revisions 行），用于边界操作写入失败时的硬回滚。

        快照中不存在的帧（拆分中途新增的片段）连同其版本/来源一并删除；
        快照中的帧按 id 原地更新/插回，保证既有版本等引用不失效。
        """
        snap_ids = {fr["id"] for fr in snapshot}
        for r in self.frames(reel_id):
            if r["id"] not in snap_ids:
                self.run("DELETE FROM frame_sources WHERE frame_id=?", (r["id"],))
                self.run("DELETE FROM frame_versions WHERE frame_id=?", (r["id"],))
                self.run("DELETE FROM frames WHERE id=?", (r["id"],))
        for fr in snapshot:
            exists = self.one("SELECT 1 FROM frames WHERE id=?", (fr["id"],))
            if exists:
                sets = ",".join("%s=?" % c for c in FRAME_COLS[1:])
                self.run("UPDATE frames SET %s WHERE id=?" % sets,
                         [fr[c] for c in FRAME_COLS[1:]] + [fr["id"]])
            else:
                ph = ",".join(["?"] * (len(FRAME_COLS) + 1))
                self.run("INSERT INTO frames(reel_id,%s) VALUES(%s)" %
                         (",".join(FRAME_COLS), ph),
                         [reel_id] + [fr[c] for c in FRAME_COLS])

    # ---- 帧边界复核 ----
    def add_boundary_op(self, reel_id, kind, reason, detail):
        cur = self.run(
            "INSERT INTO boundary_ops(reel_id, kind, reason, detail, created_at) VALUES(?,?,?,?,?)",
            (reel_id, kind, reason, json.dumps(detail, ensure_ascii=False), time.time()))
        return cur.lastrowid

    def boundary_ops(self, reel_id):
        return self.q("SELECT * FROM boundary_ops WHERE reel_id=? ORDER BY id", (reel_id,))

    def add_frame_source(self, frame_id, reel_id, kind, source_frame_id=None, op_id=None,
                         source_path="", source_filename="", region=None):
        cur = self.run(
            """INSERT INTO frame_sources(frame_id, reel_id, source_frame_id, op_id, source_path,
                                         source_filename, region, kind, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (frame_id, reel_id, source_frame_id, op_id, source_path, source_filename,
             json.dumps(region, ensure_ascii=False) if region else "", kind, time.time()))
        return cur.lastrowid

    def frame_sources(self, frame_id):
        return self.q("SELECT * FROM frame_sources WHERE frame_id=? ORDER BY id", (frame_id,))

    def source_map(self, reel_id):
        return self.q(
            """SELECT fs.* FROM frame_sources fs
               WHERE fs.id IN (SELECT MAX(id) FROM frame_sources WHERE reel_id=? GROUP BY frame_id)""",
            (reel_id,))

    # ---- 质检参数 ----
    def get_setting(self, key, default=None):
        r = self.one("SELECT value FROM qc_settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key, value):
        self.run(
            """INSERT INTO qc_settings(key, value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value)))
