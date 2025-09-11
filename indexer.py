import os
import sqlite3
from pathlib import Path
from typing import Optional
import errno
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
LOCK_DIR = DATA_DIR / "locks"
VEC_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOCK_DIR.mkdir(parents=True, exist_ok=True)

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


def _normalize_for_ngram(s: str) -> str:
    """日本語向け: 空白・改行を除去して素朴に正規化。"""
    try:
        import re
        s = s.replace("\u3000", " ")
        s = s.replace("\n", " ").replace("\r", " ")
        s = re.sub(r"\s+", "", s)
        return s
    except Exception:
        return s


def _to_ngrams(s: str, n: int = 2) -> str:
    txt = _normalize_for_ngram(s)
    if len(txt) <= n:
        return txt
    grams = [txt[i:i+n] for i in range(len(txt) - n + 1)]
    return " ".join(grams)


def _compose_index_text(filename: str, content: str) -> str:
    """ファイル名も含めて n-gram 化した文字列をFTSに投入する。"""
    base = f"{filename}\n{content or ''}"
    return _to_ngrams(base, n=2)


def _lock_path_for(folder: str) -> Path:
    safe = folder.strip("/").replace("/", "_") or "root"
    return LOCK_DIR / f"index_{safe}.lock"


def _acquire_lock(folder: str) -> Optional[Path]:
    """Create an exclusive lock file for the folder. Returns lock path or None if exists."""
    lp = _lock_path_for(folder)
    try:
        fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return lp
    except FileExistsError:
        return None


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)  # py>=3.8
    except Exception:
        pass


def build_index(dropbox_folder: str, recursive: bool = True) -> None:
    """指定フォルダ配下のファイルをダウンロード→抽出→インデックス化（FTS/FAISS）。
    recursive=True でサブフォルダも含めて再帰的に処理します。
    """
    # 二重起動防止用ロック
    lock = _acquire_lock(dropbox_folder)
    if lock is None:
        print(f"[indexer] Skip: index build already running for {dropbox_folder}")
        return
    try:
        init_schema()
        con = _connect()

        files = list(_iter_files_recursive(dropbox_folder)) if recursive else get_files_in_folder(dropbox_folder)

        cur = con.cursor()
        for f in files:
            path = f["path"]
            modified = f["modified"].isoformat() if hasattr(f["modified"], "isoformat") else str(f["modified"])  # type: ignore
            size = int(f["size"])  # type: ignore
            ext = os.path.splitext(f["name"])[-1].lower()

            # 既存メタと一致ならスキップ（重複作成回避）
            cur.execute("SELECT modified, size, ext FROM files WHERE path=?", (path,))
            row = cur.fetchone()
            if row and str(row[0]) == modified and int(row[1]) == size and str(row[2]) == ext:
                # texts 未登録なら補填する（完全スキップはしない）
                cur.execute("SELECT id FROM files WHERE path=?", (path,))
                fid_row = cur.fetchone()
                if fid_row:
                    file_id_existing = int(fid_row[0])
                    cur.execute("SELECT 1 FROM texts WHERE file_id=?", (file_id_existing,))
                    if cur.fetchone():
                        continue

            content_bytes = download_file_content(path)
            if not content_bytes:
                continue
            raw_text = extract_text_simple(content_bytes, f["name"]) or ""
            index_text = _compose_index_text(f["name"], raw_text)
            file_id = _upsert_file(con, path, modified, size, ext)
            _upsert_text(con, file_id, index_text)
            # ベクターは全文だと重いので先頭を代表ベクトルに
            head = raw_text[:1500]
            if head.strip():
                vec_store.add(file_id, head)
        con.commit()
        con.close()
        vec_store.save()
    finally:
        _release_lock(lock)


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
