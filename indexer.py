import os
import sqlite3
from pathlib import Path
from typing import Iterable, List, Tuple, Optional

import numpy as np

from file_searcher import extract_text_simple, download_file_content
from dropbox_client import get_files_in_folder, get_subfolders

# ベクター検索: faiss がなければ簡易なL2実装にフォールバック
try:
    import faiss  # type: ignore
except Exception:
    faiss = None

DATA_DIR = Path("data")
FTS_PATH = DATA_DIR / "index.sqlite"
VEC_DIR = DATA_DIR / "vector"
VEC_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

EMB_DIM = 1536  # text-embedding-3-small 既定

# Embedding 取得は openai_client に実装予定（後で差し替え）
try:
    from openai_client import get_embedding
except Exception:
    def get_embedding(text: str) -> List[float]:
        # フォールバック: 低品質だが依存を避けるための簡易ハッシュ埋め込み
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        return rng.normal(size=EMB_DIM).astype(np.float32).tolist()


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(FTS_PATH))
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con


def init_schema() -> None:
    con = _connect()
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, path TEXT UNIQUE, modified TEXT, size INTEGER, ext TEXT)"
    )
    cur.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS texts USING fts5(content, file_id UNINDEXED, tokenize='unicode61')"
    )
    con.commit()
    con.close()


def _upsert_file(con: sqlite3.Connection, path: str, modified: str, size: int, ext: str) -> int:
    cur = con.cursor()
    cur.execute(
        "INSERT INTO files(path, modified, size, ext) VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET modified=excluded.modified,size=excluded.size,ext=excluded.ext",
        (path, modified, size, ext),
    )
    cur.execute("SELECT id FROM files WHERE path=?", (path,))
    row = cur.fetchone()
    return int(row[0])


def _upsert_text(con: sqlite3.Connection, file_id: int, content: str) -> None:
    cur = con.cursor()
    cur.execute("DELETE FROM texts WHERE file_id=?", (file_id,))
    cur.execute("INSERT INTO texts(content, file_id) VALUES(?,?)", (content, file_id))


# ベクターインデックス（FAISS）
class VectorStore:
    def __init__(self, dim: int, path: Path) -> None:
        self.dim = dim
        self.path = path
        self.ids: List[int] = []
        if faiss is not None and (path / "index.faiss").exists():
            self.index = faiss.read_index(str(path / "index.faiss"))
            self.ids = list(np.load(path / "ids.npy"))
        else:
            self.index = faiss.IndexFlatIP(dim) if faiss is not None else None

    def add(self, file_id: int, text: str) -> None:
        vec = np.array(get_embedding(text), dtype=np.float32)
        vec = vec.reshape(1, -1)
        if faiss is not None and self.index is not None:
            self.index.add(vec)
            self.ids.append(file_id)
        # フォールバックは未保存（検索時はFTSで対応）

    def save(self) -> None:
        if faiss is not None and self.index is not None:
            faiss.write_index(self.index, str(self.path / "index.faiss"))
            np.save(self.path / "ids.npy", np.array(self.ids, dtype=np.int64))

    def search(self, query: str, k: int = 10) -> List[int]:
        if faiss is None or self.index is None or self.index.ntotal == 0:
            return []
        q = np.array(get_embedding(query), dtype=np.float32).reshape(1, -1)
        sims, idxs = self.index.search(q, k)
        result_ids: List[int] = []
        for r in idxs[0]:
            if r == -1:
                continue
            result_ids.append(self.ids[r])
        return result_ids


vec_store = VectorStore(EMB_DIM, VEC_DIR)


def _iter_files_recursive(root: str):
    """指定ルート配下のファイルを（サブフォルダも含めて）逐次取得"""
    stack = [root]
    while stack:
        cur = stack.pop()
        # 直下のファイル
        for f in get_files_in_folder(cur):
            yield f
        # サブフォルダを探索
        try:
            subs = get_subfolders(cur) or []
        except Exception:
            subs = []
        for s in subs:
            p = s.get("full_path") or s.get("path")
            if p:
                stack.append(p)


def build_index(dropbox_folder: str, recursive: bool = True) -> None:
    """指定フォルダ配下のファイルをダウンロード→抽出→インデックス化（FTS/FAISS）。
    recursive=True でサブフォルダも含めて再帰的に処理します。
    """
    init_schema()
    con = _connect()

    files = list(_iter_files_recursive(dropbox_folder)) if recursive else get_files_in_folder(dropbox_folder)

    for f in files:
        path = f["path"]
        modified = f["modified"].isoformat() if hasattr(f["modified"], "isoformat") else str(f["modified"])  # type: ignore
        size = int(f["size"])  # type: ignore
        ext = os.path.splitext(f["name"])[-1].lower()

        content_bytes = download_file_content(path)
        if not content_bytes:
            continue
        text = extract_text_simple(content_bytes, f["name"]) or ""
        file_id = _upsert_file(con, path, modified, size, ext)
        _upsert_text(con, file_id, text)
        # ベクターは全文だと重いので先頭を代表ベクトルに
        head = text[:1500]
        if head.strip():
            vec_store.add(file_id, head)
    con.commit()
    con.close()
    vec_store.save()


def search_fts(query: str, limit: int = 20) -> List[Tuple[int, str]]:
    con = _connect()
    cur = con.cursor()
    cur.execute(
        "SELECT files.id, files.path FROM texts JOIN files ON texts.file_id=files.id WHERE texts MATCH ? LIMIT ?",
        (query, limit),
    )
    rows = cur.fetchall()
    con.close()
    return [(int(r[0]), str(r[1])) for r in rows]


def search_vector(query: str, k: int = 10) -> List[Tuple[int, str]]:
    ids = vec_store.search(query, k)
    if not ids:
        return []
    con = _connect()
    cur = con.cursor()
    qmarks = ",".join(["?"] * len(ids))
    cur.execute(f"SELECT id, path FROM files WHERE id IN ({qmarks})", ids)
    rows = cur.fetchall()
    con.close()
    return [(int(r[0]), str(r[1])) for r in rows]


def get_paths_by_ids(ids: List[int]) -> List[str]:
    if not ids:
        return []
    con = _connect()
    cur = con.cursor()
    qmarks = ",".join(["?"] * len(ids))
    cur.execute(f"SELECT path FROM files WHERE id IN ({qmarks})", ids)
    rows = cur.fetchall()
    con.close()
    return [str(r[0]) for r in rows]
