import os
import sqlite3
from pathlib import Path
from typing import Optional
import errno
from typing import Iterable, List, Tuple, Optional, Callable
from datetime import datetime
import time

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
    con.execute("PRAGMA busy_timeout=2000;")
    return con


def init_schema() -> None:
    con = _connect()
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, path TEXT UNIQUE, modified TEXT, size INTEGER, ext TEXT)"
    )
    # 本文実体（1箇所のみ）
    cur.execute(
        "CREATE TABLE IF NOT EXISTS contents (file_id INTEGER PRIMARY KEY, content TEXT)"
    )
    cur.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS texts USING fts5(content, tokenize='unicode61', content='')"
    )
    # 日本語向け: 2-gram を事前生成して登録するテーブル
    cur.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS texts_ng USING fts5(content, tokenize='unicode61', content='')"
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
    # 本文は通常テーブルに集約
    cur.execute(
        "INSERT INTO contents(file_id, content) VALUES(?,?) ON CONFLICT(file_id) DO UPDATE SET content=excluded.content",
        (file_id, content),
    )
    # FTS（contentless）は rowid=files.id でインデックスを更新
    cur.execute("DELETE FROM texts WHERE rowid=?", (file_id,))
    cur.execute("INSERT INTO texts(rowid, content) VALUES(?,?)", (file_id, content))


def _upsert_text_ng(con: sqlite3.Connection, file_id: int, content: str) -> None:
    cur = con.cursor()
    cur.execute("DELETE FROM texts_ng WHERE rowid=?", (file_id,))
    cur.execute("INSERT INTO texts_ng(rowid, content) VALUES(?,?)", (file_id, content))


# 2-gram 生成（重複あり、スペース区切り）

def to_bigrams(s: str) -> str:
    s = (s or "").replace("\n", "").replace("\r", "")
    if len(s) < 2:
        return s
    grams = [s[i:i+2] for i in range(len(s)-1)]
    return " ".join(grams)


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

DEBUG_INDEX = os.getenv("INDEX_DEBUG", "0") == "1"
LOCK_MAX_AGE_SEC = int(os.getenv("INDEX_LOCK_MAX_AGE_SEC", "600"))  # 既定10分

def _iter_files_recursive(root: str, exclude_prefixes: Optional[List[str]] = None):
    """指定ルート配下のファイルを（サブフォルダも含めて）逐次取得。
    exclude_prefixes が与えられた場合、その配下（サブも含む）は除外。
    """
    def _is_excluded(path: str) -> bool:
        if not exclude_prefixes:
            return False
        p = (path or "").rstrip("/")
        for ex in exclude_prefixes:
            exn = (ex or "").rstrip("/")
            if not exn:
                continue
            if p == exn or p.startswith(exn + "/"):
                return True
        return False

    stack = [root]
    while stack:
        cur = stack.pop()
        # 直下のファイル
        if not _is_excluded(cur):
            for f in get_files_in_folder(cur):
                fp = f.get("path") or ""
                if not _is_excluded(fp):
                    yield f
        # サブフォルダを探索
        try:
            subs = get_subfolders(cur) or []
        except Exception:
            subs = []
        for s in subs:
            p = s.get("full_path") or s.get("path")
            if p and not _is_excluded(p):
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


def backfill_texts_ng(batch_size: int = 1000) -> int:
    """texts_ng が空、または不足している場合に、files/contents から後追いで補完する。
    既存DBに texts_ng が未作成だった履歴があるとカウント0のままになるための救済。
    戻り値は補完した件数。
    """
    init_schema()
    con = _connect()
    cur = con.cursor()
    total = 0
    while True:
        cur.execute(
            """
            SELECT f.id, f.path, c.content
            FROM files f
            JOIN contents c ON c.file_id = f.id
            LEFT JOIN texts_ng n ON n.rowid = f.id
            WHERE n.rowid IS NULL
            LIMIT ?
            """,
            (batch_size,),
        )
        rows = cur.fetchall()
        if not rows:
            break
        for fid, path, content in rows:
            try:
                filename = os.path.basename(str(path))
                index_text = _compose_index_text(filename, content or "")
                _upsert_text_ng(con, int(fid), index_text)
                total += 1
                if DEBUG_INDEX:
                    print(f"[indexer] ngram backfill: {path}")
            except Exception:
                # 1件失敗しても続行
                pass
        con.commit()
    con.close()
    return total


def _lock_path_for(folder: str) -> Path:
    safe = folder.strip("/").replace("/", "_") or "root"
    return LOCK_DIR / f"index_{safe}.lock"


def _is_lock_stale(lock_path: Path, max_age_sec: int = LOCK_MAX_AGE_SEC) -> bool:
    try:
        age = time.time() - lock_path.stat().st_mtime
        return age > max_age_sec
    except Exception:
        return True


def _acquire_lock(folder: str) -> Optional[Path]:
    """Create an exclusive lock file for the folder. Returns lock path or None if exists and fresh."""
    lp = _lock_path_for(folder)
    # 既存ロックが古い場合は回収
    if lp.exists() and _is_lock_stale(lp, max_age_sec=LOCK_MAX_AGE_SEC):
        try:
            lp.unlink()
        except Exception:
            pass
    try:
        fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(f"pid={os.getpid()} ts={int(time.time())}\n")
        return lp
    except FileExistsError:
        return None


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)  # py>=3.8
    except Exception:
        pass


def is_index_locked(folder: str) -> dict:
    """Return lock status for a folder. {locked: bool, age_sec: float}"""
    lp = _lock_path_for(folder)
    if not lp.exists():
        return {"locked": False, "age_sec": 0.0}
    try:
        age = time.time() - lp.stat().st_mtime
    except Exception:
        age = -1.0
    # 古ければlocked=False扱いにしてよいが、UIでは解除ボタンを出すためlockedとして返す
    return {"locked": True, "age_sec": age, "stale": _is_lock_stale(lp, LOCK_MAX_AGE_SEC)}


def force_release_lock(folder: str) -> bool:
    lp = _lock_path_for(folder)
    try:
        lp.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def build_index(
    dropbox_folder: str,
    recursive: bool = True,
    exclude_prefixes: Optional[List[str]] = None,
    include_prefixes: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> dict:
    """指定フォルダ配下のファイルをダウンロード→抽出→インデックス化（FTS/FAISS）。
    recursive=True でサブフォルダも含めて再帰的に処理します。
    """
    # 二重起動防止用ロック
    lock = _acquire_lock(dropbox_folder)
    if lock is None:
        # 既に実行中、または中断によりロックが残っている
        if DEBUG_INDEX:
            print(f"[indexer] Skip: index build already running for {dropbox_folder}", flush=True)
        return {"started": False, "reason": "locked"}
    try:
        init_schema()
        con = _connect()
        cur = con.cursor()

        start_ts = time.perf_counter()
        num_indexed = 0
        num_skipped = 0

        if recursive:
            if include_prefixes:
                roots: List[str] = []
                base = (dropbox_folder or "").rstrip("/")
                for inc in include_prefixes:
                    incn = (inc or "").rstrip("/")
                    if not incn:
                        continue
                    if incn == base or incn.startswith(base + "/"):
                        roots.append(incn)
                files = []
                for r in roots:
                    files.extend(list(_iter_files_recursive(r, exclude_prefixes)))
            else:
                files = list(_iter_files_recursive(dropbox_folder, exclude_prefixes))
        else:
            base_files = get_files_in_folder(dropbox_folder)
            files = []
            for f in base_files:
                p = (f.get("path") or "")
                ok = True
                if include_prefixes:
                    ok = any(p == inc.rstrip("/") or p.startswith(inc.rstrip("/") + "/") for inc in include_prefixes)
                if ok and exclude_prefixes:
                    if any(p == ex.rstrip("/") or p.startswith(ex.rstrip("/") + "/") for ex in exclude_prefixes):
                        ok = False
                if ok:
                    files.append(f)

        total = len(files)
        done = 0
        if progress_cb is not None:
            try:
                progress_cb(0, total, "")
            except Exception:
                pass

        for f in files:
            path = f["path"]
            modified = f["modified"].isoformat() if hasattr(f["modified"], "isoformat") else str(f["modified"])  # type: ignore
            size = int(f["size"])  # type: ignore
            ext = os.path.splitext(f["name"])[-1].lower()

            # 既存メタと一致時は、空テキストやn-gram未登録なら再作成。それ以外はスキップ
            cur.execute("SELECT id, modified, size, ext FROM files WHERE path= ?", (path,))
            row = cur.fetchone()
            if row and str(row[1]) == modified and int(row[2]) == size and str(row[3]) == ext:
                file_id_existing = int(row[0])
                cur.execute("SELECT COALESCE(length(content),0) FROM contents WHERE file_id= ?", (file_id_existing,))
                len_row = cur.fetchone()
                has_nonempty_text = bool(len_row and int(len_row[0]) > 0)
                cur.execute("SELECT 1 FROM texts_ng WHERE rowid= ?", (file_id_existing,))
                has_ng = bool(cur.fetchone())
                if has_nonempty_text and has_ng:
                    if DEBUG_INDEX:
                        print(f"[indexer] skip (up-to-date): {path}", flush=True)
                    num_skipped += 1
                    done += 1
                    if progress_cb is not None:
                        try:
                            progress_cb(done, total, path)
                        except Exception:
                            pass
                    continue

            content_bytes = download_file_content(path)
            if not content_bytes:
                num_skipped += 1
                continue
            raw_text = extract_text_simple(content_bytes, f["name"]) or ""
            file_id = _upsert_file(con, path, modified, size, ext)
            # 通常FTS: 生テキスト
            _upsert_text(con, file_id, raw_text)
            # n-gram FTS: ファイル名+本文を2-gram化
            index_text = _compose_index_text(f["name"], raw_text)
            _upsert_text_ng(con, file_id, index_text)
            # ベクターは全文だと重いので先頭を代表ベクトルに
            head = raw_text[:1500]
            if head.strip():
                vec_store.add(file_id, head)
            if DEBUG_INDEX:
                print(f"[indexer] indexed: {path}", flush=True)
            num_indexed += 1
            done += 1
            if progress_cb is not None:
                try:
                    progress_cb(done, total, path)
                except Exception:
                    pass
        con.commit()
        con.close()
        vec_store.save()
        duration = time.perf_counter() - start_ts
        return {"started": True, "indexed": num_indexed, "skipped": num_skipped, "duration_sec": duration, "total": total}
    finally:
        _release_lock(lock)


def search_fts(query: str, limit: int = 20, folder_prefix: Optional[str] = None, timeout_ms: int = 1500) -> List[Tuple[int, str]]:
    init_schema()
    con = _connect()
    cur = con.cursor()
    start = time.perf_counter()
    def _progress() -> int:
        if (time.perf_counter() - start) * 1000.0 > timeout_ms:
            return 1
        return 0
    con.set_progress_handler(_progress, 10000)
    if folder_prefix:
        like = folder_prefix.rstrip("/") + "/%"
        cur.execute(
            "SELECT files.id, files.path FROM texts JOIN files ON files.id = texts.rowid WHERE texts MATCH ? AND files.path LIKE ? LIMIT ?",
            (query, like, limit),
        )
    else:
        cur.execute(
            "SELECT files.id, files.path FROM texts JOIN files ON files.id = texts.rowid WHERE texts MATCH ? LIMIT ?",
            (query, limit),
        )
    rows = cur.fetchall()
    con.set_progress_handler(None, 0)
    con.close()
    return [(int(r[0]), str(r[1])) for r in rows]


def search_fts_ng(query: str, limit: int = 20, folder_prefix: Optional[str] = None, timeout_ms: int = 1500) -> List[Tuple[int, str]]:
    init_schema()
    # クエリもインデックス時と同じ正規化→2-gram化
    grams_str = _to_ngrams(query, n=2)
    if not grams_str or len(query) < 2:
        return search_fts(query, limit, folder_prefix)
    tokens = grams_str.split()
    # OR で任意のgram一致に緩和（取りこぼし削減）
    q = " OR ".join(tokens)
    con = _connect()
    cur = con.cursor()
    start = time.perf_counter()
    def _progress() -> int:
        if (time.perf_counter() - start) * 1000.0 > timeout_ms:
            return 1
        return 0
    con.set_progress_handler(_progress, 10000)
    if folder_prefix:
        like = folder_prefix.rstrip("/") + "/%"
        cur.execute(
            "SELECT files.id, files.path FROM texts_ng JOIN files ON files.id = texts_ng.rowid WHERE texts_ng MATCH ? AND files.path LIKE ? LIMIT ?",
            (q, like, limit),
        )
    else:
        cur.execute(
            "SELECT files.id, files.path FROM texts_ng JOIN files ON files.id = texts_ng.rowid WHERE texts_ng MATCH ? LIMIT ?",
            (q, limit),
        )
    rows = cur.fetchall()
    con.set_progress_handler(None, 0)
    con.close()
    return [(int(r[0]), str(r[1])) for r in rows]


def search_fts_ng_exact(query: str, limit: int = 20, folder_prefix: Optional[str] = None, timeout_ms: int = 2000) -> List[Tuple[int, str]]:
    """n-gram候補をFTSで取得しつつ、本文にクエリ文字列が実際に含まれるものに限定。
    SQLiteの INSTR を使った厳密サブストリング判定（Unicode対応）。
    """
    init_schema()
    con = _connect()
    cur = con.cursor()
    start = time.perf_counter()
    def _progress() -> int:
        if (time.perf_counter() - start) * 1000.0 > timeout_ms:
            return 1
        return 0
    con.set_progress_handler(_progress, 10000)
    # クエリも正規化→2-gram化（候補拡張）。
    grams_str = _to_ngrams(query, n=2)
    if grams_str and len(query) >= 2:
        tokens = grams_str.split()
        q = " OR ".join(tokens)
        if folder_prefix:
            like = folder_prefix.rstrip("/") + "/%"
            cur.execute(
                (
                    "SELECT files.id, files.path "
                    "FROM texts_ng "
                    "JOIN files ON texts_ng.rowid = files.id "
                    "JOIN contents c ON c.file_id = files.id "
                    "WHERE texts_ng MATCH ? AND instr(c.content, ?) > 0 AND files.path LIKE ? "
                    "LIMIT ?"
                ),
                (q, query, like, limit),
            )
        else:
            cur.execute(
                (
                    "SELECT files.id, files.path "
                    "FROM texts_ng "
                    "JOIN files ON texts_ng.rowid = files.id "
                    "JOIN contents c ON c.file_id = files.id "
                    "WHERE texts_ng MATCH ? AND instr(c.content, ?) > 0 "
                    "LIMIT ?"
                ),
                (q, query, limit),
            )
    else:
        # 短いクエリは n-gram を使わず、本文の厳密一致のみ
        if folder_prefix:
            like = folder_prefix.rstrip("/") + "/%"
            cur.execute(
                (
                    "SELECT files.id, files.path "
                    "FROM contents c JOIN files ON c.file_id = files.id "
                    "WHERE instr(c.content, ?) > 0 AND files.path LIKE ? "
                    "LIMIT ?"
                ),
                (query, like, limit),
            )
        else:
            cur.execute(
                (
                    "SELECT files.id, files.path "
                    "FROM contents c JOIN files ON c.file_id = files.id "
                    "WHERE instr(c.content, ?) > 0 "
                    "LIMIT ?"
                ),
                (query, limit),
            )
    rows = cur.fetchall()
    con.set_progress_handler(None, 0)
    con.close()
    return [(int(r[0]), str(r[1])) for r in rows]

def search_vector(query: str, k: int = 10, folder_prefix: Optional[str] = None) -> List[Tuple[int, str]]:
    ids = vec_store.search(query, k)
    if not ids:
        return []
    con = _connect()
    cur = con.cursor()
    qmarks = ",".join(["?"] * len(ids))
    if folder_prefix:
        like = folder_prefix.rstrip("/") + "/%"
        cur.execute(f"SELECT id, path FROM files WHERE id IN ({qmarks}) AND path LIKE ?", (*ids, like))
    else:
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


def get_files_by_ids(ids: List[int]) -> List[dict]:
    if not ids:
        return []
    con = _connect()
    cur = con.cursor()
    qmarks = ",".join(["?"] * len(ids))
    cur.execute(f"SELECT id, path, modified, size, ext FROM files WHERE id IN ({qmarks})", ids)
    rows = cur.fetchall()
    con.close()
    results: List[dict] = []
    for fid, path, modified, size, ext in rows:
        try:
            mod_dt = datetime.fromisoformat(str(modified)) if modified else None
        except Exception:
            mod_dt = None
        results.append({
            'id': int(fid),
            'name': os.path.basename(str(path)),
            'path': str(path),
            'modified': mod_dt or str(modified),
            'size': int(size) if size is not None else 0,
            'ext': str(ext) if ext is not None else ''
        })
    # 入力順に並べ替え
    order = {i: idx for idx, i in enumerate(ids)}
    results.sort(key=lambda d: order.get(d.get('id', -1), 1_000_000))
    return results


def count_indexed_files_in(folder: str) -> int:
    """指定フォルダ配下でインデックス済みのファイル件数を返す。未作成判定に利用。"""
    init_schema()
    con = _connect()
    cur = con.cursor()
    folder = (folder or "").rstrip("/")
    if not folder:
        cur.execute("SELECT COUNT(*) FROM files")
        n = int(cur.fetchone()[0])
    else:
        like = folder + "/%"
        cur.execute("SELECT COUNT(*) FROM files WHERE path LIKE ?", (like,))
        row = cur.fetchone()
        n = int(row[0]) if row else 0
    con.close()
    return n


def get_storage_bytes() -> dict:
    """インデックス関連ファイルのサイズをバイトで返す。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    def size_of(p: Path) -> int:
        try:
            return p.stat().st_size
        except FileNotFoundError:
            return 0
    sqlite_b = size_of(FTS_PATH)
    wal_b = size_of(FTS_PATH.with_suffix(FTS_PATH.suffix + "-wal"))
    shm_b = size_of(FTS_PATH.with_suffix(FTS_PATH.suffix + "-shm"))
    vec_b = 0
    if VEC_DIR.exists():
        for child in VEC_DIR.glob("**/*"):
            if child.is_file():
                vec_b += size_of(child)
    total = sqlite_b + wal_b + shm_b + vec_b
    return {"sqlite": sqlite_b, "wal": wal_b, "shm": shm_b, "vector": vec_b, "total": total}


def reset_index() -> int:
    """インデックス（SQLite/FTS/FAISS/ロック）を全削除し、解放バイト数を返す。"""
    before = get_storage_bytes().get("total", 0)
    # 既存接続は呼び出し側で閉じられている前提
    for p in [FTS_PATH, FTS_PATH.with_suffix(FTS_PATH.suffix + "-wal"), FTS_PATH.with_suffix(FTS_PATH.suffix + "-shm")]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
    # ベクター
    if VEC_DIR.exists():
        for child in VEC_DIR.glob("**/*"):
            try:
                if child.is_file():
                    child.unlink()
            except Exception:
                pass
    # ロック
    if LOCK_DIR.exists():
        for child in LOCK_DIR.glob("index_*.lock"):
            try:
                child.unlink()
            except Exception:
                pass
    # 再初期化
    init_schema()
    after = get_storage_bytes().get("total", 0)
    return max(0, before - after)
