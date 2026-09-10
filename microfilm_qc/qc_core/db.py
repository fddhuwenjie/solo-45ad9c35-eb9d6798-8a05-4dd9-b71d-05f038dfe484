"""SQLite 数据层：卷盘、帧、告警、可撤销修订。"""
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
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frames_reel ON frames(reel_id, position);
CREATE INDEX IF NOT EXISTS idx_warn_reel ON warnings(reel_id);
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
        self.conn.commit()

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

    # ---- 修订（撤销） ----
    def snapshot(self, reel_id):
        rows = self.frames(reel_id)
        return [{c: r[c] for c in FRAME_COLS} for r in rows]

    def save_revision(self, reel_id, action):
        snap = json.dumps(self.snapshot(reel_id), ensure_ascii=False)
        self.run("INSERT INTO revisions(reel_id, action, snapshot, created_at) VALUES(?,?,?,?)",
                 (reel_id, action, snap, time.time()))

    def undo(self, reel_id):
        rev = self.one("SELECT * FROM revisions WHERE reel_id=? ORDER BY id DESC", (reel_id,))
        if not rev:
            return None
        snap = json.loads(rev["snapshot"])
        self.run("DELETE FROM frames WHERE reel_id=?", (reel_id,))
        for fr in snap:
            cols = FRAME_COLS
            self.run("INSERT INTO frames(reel_id,%s) VALUES(%s)" %
                     (",".join(cols), ",".join(["?"] * (len(cols) + 1))),
                     [reel_id] + [fr[c] for c in cols])
        self.run("DELETE FROM revisions WHERE id=?", (rev["id"],))
        self.conn.commit()
        return rev["action"]

    def revisions(self, reel_id):
        return self.q("SELECT id, action, created_at FROM revisions WHERE reel_id=? ORDER BY id DESC",
                      (reel_id,))
