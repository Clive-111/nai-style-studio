"""SQLite collections and content-addressed local image storage."""
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path


class Library:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.images = self.directory / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS styles(
                    id INTEGER PRIMARY KEY, seed_key TEXT UNIQUE,
                    name TEXT NOT NULL, description TEXT NOT NULL, recipe TEXT NOT NULL,
                    cover_id INTEGER REFERENCES examples(id));
                CREATE TABLE IF NOT EXISTS examples(
                    id INTEGER PRIMARY KEY, style_id INTEGER NOT NULL REFERENCES styles(id),
                    filename TEXT NOT NULL, digest TEXT, kind TEXT NOT NULL,
                    metadata TEXT NOT NULL, UNIQUE(style_id, digest));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.directory / "library.sqlite3", timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def store_image(self, data, extension="png"):
        digest = hashlib.sha256(data).hexdigest()
        path = self.images / (digest + "." + extension)
        if not path.exists():
            with tempfile.NamedTemporaryFile(dir=self.images, delete=False) as tmp:
                temp = Path(tmp.name)
                try:
                    tmp.write(data)
                except BaseException:
                    tmp.close()
                    temp.unlink(missing_ok=True)
                    raise
            try:
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        return digest

    def seed(self, records):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in records:
                if db.execute("SELECT 1 FROM styles WHERE seed_key=?", (item["key"],)).fetchone():
                    continue
                sid = db.execute("INSERT INTO styles(seed_key,name,description,recipe) VALUES(?,?,?,?)",
                                 tuple(item[k] for k in ("key", "name", "description", "recipe"))).lastrowid
                path = item.get("image_path")
                digest = self.store_image(Path(path).read_bytes()) if path else None
                kind = item.get("image_kind", "original") if path else "archive"
                self.add_example(db, sid, item.get("filename") or "", digest, kind, item["metadata"])

    @staticmethod
    def add_example(db, sid, filename, digest, kind, metadata):
        eid = db.execute(
            "INSERT INTO examples(style_id,filename,digest,kind,metadata) VALUES(?,?,?,?,?)",
            (sid, filename, digest, kind, json.dumps(metadata, ensure_ascii=False)),
        ).lastrowid
        if digest:
            db.execute("UPDATE styles SET cover_id=COALESCE(cover_id,?) WHERE id=?", (eid, sid))

    @staticmethod
    def summaries(db, sid=None):
        sql = """SELECT s.id,s.name,s.description,s.recipe,s.cover_id,
                     COALESCE(NULLIF(trim(json_extract(c.metadata,'$.model')), ''),
                              trim(json_extract(c.metadata,'$.source')), '') AS cover_model,
                     '/images/' || c.digest || '.' || COALESCE(json_extract(c.metadata,'$.image_format'),'png') AS cover_url,
                     (SELECT count(digest) FROM examples e WHERE e.style_id=s.id) AS example_count
                 FROM styles s LEFT JOIN examples c ON c.id=s.cover_id"""
        if sid is None:
            return [dict(row) for row in db.execute(sql + " ORDER BY s.id")]
        row = db.execute(sql + " WHERE s.id=?", (sid,)).fetchone()
        if row is None:
            raise KeyError("找不到这套收藏")
        return dict(row)

    def detail(self, sid):
        with self.connection() as db:
            db.execute("BEGIN")
            style = self.summaries(db, sid)
            style["examples"] = [
                dict(id=e["id"], filename=e["filename"], image_kind=e["kind"],
                     image_url=f"/images/{e['digest']}.{meta.get('image_format','png')}" if e["digest"] else None,
                     metadata=meta)
                for e in db.execute("SELECT * FROM examples WHERE style_id=? ORDER BY id", (sid,))
                for meta in (json.loads(e["metadata"]),)
            ]
            return style

    def save(self, target, fields, image, metadata, filename):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if target is None:
                target = db.execute("INSERT INTO styles(name,description,recipe) VALUES(?,?,?)", fields).lastrowid
            else:
                self.summaries(db, target)
            digest = self.store_image(image, metadata.get("image_format", "png"))
            existing = db.execute("SELECT id FROM examples WHERE style_id=? AND digest=?", (target, digest)).fetchone()
            if existing:
                return {"id": target, "duplicate": True}
            # Upgrade the TXT archive or screen preview when its matching original arrives.
            for row in db.execute("SELECT * FROM examples WHERE style_id=? AND kind != 'original'", (target,)):
                old = json.loads(row["metadata"])
                if (metadata.get("prompt") and metadata.get("seed") is not None
                        and str(old.get("seed")) == str(metadata["seed"])
                        and old.get("prompt", "").replace("\r\n", "\n").strip() == metadata["prompt"].replace("\r\n", "\n").strip()):
                    db.execute("UPDATE examples SET digest=?,filename=?,kind='original',metadata=? WHERE id=?",
                               (digest, filename, json.dumps(old | metadata, ensure_ascii=False), row["id"]))
                    db.execute("UPDATE styles SET cover_id=COALESCE(cover_id,?) WHERE id=?", (row["id"], target))
                    return {"id": target, "duplicate": False}
            self.add_example(db, target, filename, digest, "original", metadata)
            return {"id": target, "duplicate": False}

    def edit(self, sid, fields=None, cover=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.summaries(db, sid)
            if fields is not None:
                db.execute("UPDATE styles SET name=?,description=?,recipe=? WHERE id=?", (*fields, sid))
            if cover is not None:
                if not db.execute("SELECT id FROM examples WHERE id=? AND style_id=? AND digest IS NOT NULL",
                                  (cover, sid)).fetchone():
                    raise ValueError("请选择本套收藏中的例图")
                db.execute("UPDATE styles SET cover_id=? WHERE id=?", (cover, sid))
