import streamlit as st
import PyPDF2
import io
import openpyxl
import docx
import os
import time
import config
from dropbox_client import test_connection, get_dropbox_folders, get_subfolders, get_files_in_folder
from openai_client import test_openai_connection, process_user_instruction
from file_searcher import search_files_comprehensive, download_file_content, extract_text_simple
from keyword_extractor import extract_keywords
from urllib.parse import quote
from indexer import build_index, search_fts, search_vector
from indexer import search_fts_ng, search_fts_ng_exact, backfill_texts_ng, count_indexed_files_in
from indexer import get_storage_bytes, reset_index, get_files_by_ids
from indexer import is_index_locked, force_release_lock


ROOT_PATH = getattr(config, "ROOT_PATH", "")

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# --- 追加: OCRフォールバックを内包したテキスト抽出を使って概要を生成 ---
# extract_text_simple 側でOCRフォールバックが実装されているため、
# ここでは抽出済みテキストをLLMで要約するだけで良い。

def _list_files_recursive(root_path: str):
    """指定フォルダ配下（サブフォルダ含む）のファイル一覧を返す（深さ優先）。"""
    stack = [root_path]
    out = []
    seen = set()
    while stack:
        cur = stack.pop()
        try:
            for f in get_files_in_folder(cur) or []:
                p = f.get('path')
                if p and p not in seen:
                    out.append(f)
                    seen.add(p)
            for s in get_subfolders(cur) or []:
                p = s.get('full_path') or s.get('path')
                if p:
                    stack.append(p)
        except Exception:
            pass
    return out

@st.cache_data(show_spinner=False, ttl=60)
def _list_files_recursive_cached(root_path: str):
    return _list_files_recursive(root_path)

def _is_excluded_path(p: str) -> bool:
    try:
        p = (p or "").rstrip('/')
        for ex in st.session_state.get("excluded_folders", set()):
            exn = (ex or "").rstrip('/')
            if p == exn or p.startswith(exn + "/"):
                return True
        return False
    except Exception:
        return False

def get_file_summary(file_path: str, file_name: str) -> str:
    """ファイル内容をOCRを含む抽出で取得し、短い日本語要約を返す。"""
    try:
        file_content = download_file_content(file_path)
        if not file_content:
            return "ファイルの内容を取得できませんでした。"

        text = extract_text_simple(file_content, file_name) or ""
        if not text.strip():
            return "ファイルの内容を読み取れませんでした。画像ベースのPDFの可能性があります。"

        prompt = (
            "以下の文書を日本語で100文字以内の要約にしてください。\n"
            f"ファイル名: {file_name}\n\n"
            f"本文(先頭のみ):\n{text[:2500]}\n\n"
            "要約:"
        )
        summary = process_user_instruction(prompt) or ""
        return summary.strip() or "要約を生成できませんでした。"
    except Exception as e:
        return f"概要の生成中にエラーが発生しました: {e}"


def search_from_filtered_files(filtered_files, user_input):
    """絞り込まれたファイルリストから検索"""
    # キーワード抽出
    keywords = extract_keywords(user_input)
    if not keywords:
        return []
    
    top_keyword = max(keywords, key=lambda x: x['relevance'])
    search_term = top_keyword['keyword']
    
    results = []
    for file in filtered_files:
        # ファイル名で検索
        if search_term.lower() in file['name'].lower():
            results.append({
                'file': file,
                'match_type': 'filename',
                'search_term': search_term
            })
        else:
            # ファイル内容で検索
            file_content = download_file_content(file['path'])
            if file_content:
                text = extract_text_simple(file_content, file['name'])
                if search_term.lower() in text.lower():
                    results.append({
                        'file': file,
                        'match_type': 'content',
                        'search_term': search_term
                    })
    
    return results

st.title("DropBox ファイル検索システム")

# セッション状態の初期化
if "messages" not in st.session_state:
    st.session_state.messages = []
if "filtered_files" not in st.session_state:
    st.session_state.filtered_files = None
if "selected_file" not in st.session_state:
    st.session_state.selected_file = None
if "file_content_preview" not in st.session_state:
    st.session_state.file_content_preview = None
if "file_content_preview_images" not in st.session_state:
    st.session_state.file_content_preview_images = None
if "current_folder" not in st.session_state:
    st.session_state.current_folder = None
if "selected_folder_prev" not in st.session_state:
    st.session_state.selected_folder_prev = None
if "file_content_preview_limit" not in st.session_state:
    st.session_state.file_content_preview_limit = 2000
if "index_warned_for" not in st.session_state:
    st.session_state.index_warned_for = None
if "included_folders" not in st.session_state:
    st.session_state.included_folders = set()

# DropBox APIでフォルダ取得
base_path = ROOT_PATH  # 例: "/三友工業株式会社 Dropbox"
folder_list = [base_path] + get_dropbox_folders(base_path)  # ルート自身＋直下のフォルダ

# 既存のフォルダ選択コードの後に追加
if folder_list:
    name = test_connection()

    st.sidebar.write('🟢DropBox接続成功')
    st.sidebar.success(f"ユーザー名: {name}")

    selected_folder = st.sidebar.selectbox(
        "検索対象フォルダを選択",
        folder_list,
        index=0
    )
    
    # インデックス存在チェックのヘルパ
    def _ensure_index_warning(target_folder: str) -> None:
        # 同一フォルダで重複表示しない
        if st.session_state.index_warned_for == target_folder:
            return
        try:
            n_indexed = count_indexed_files_in(target_folder)
        except Exception:
            n_indexed = 0
        if n_indexed == 0:
            with st.sidebar:
                st.warning("このフォルダは未インデックスです。作成しますか？")
                if st.button("📚 いま作成する", key=f"btn_build_index_{target_folder}"):
                    with st.spinner("インデックスを作成しています..."):
                        include = sorted(list(st.session_state.get("included_folders", set())))
                        before = 0
                        try:
                            before = count_indexed_files_in(target_folder)
                        except Exception:
                            before = 0
                        result = build_index(target_folder, include_prefixes=include)
                        try:
                            backfill_texts_ng()
                        except Exception:
                            pass
                    try:
                        if not result.get("started"):
                            st.warning("別のインデックス処理が実行中のためスキップしました。しばらくしてから再試行してください。")
                        else:
                            after = count_indexed_files_in(target_folder)
                            delta = max(0, after - before)
                            st.success(
                                f"インデックス作成が完了しました（新規 {delta} 件 / 追加 {result.get('indexed',0)} 件 / スキップ {result.get('skipped',0)} 件 / {result.get('duration_sec',0):.1f}s）"
                            )
                    except Exception:
                        st.success("インデックス作成が完了しました")
        # 表示済みとして記録
        st.session_state.index_warned_for = target_folder

    # フォルダ選択の変更検知と現在フォルダの初期化
    if st.session_state.selected_folder_prev != selected_folder:
        st.session_state.selected_folder_prev = selected_folder
        st.session_state.current_folder = selected_folder
        st.session_state.filtered_files = None
        # 別フォルダの古いpathクエリをクリア
        try:
            _qp = st.query_params
            if 'path' in _qp:
                del _qp['path']
        except Exception:
            pass
    
    # 選択したフォルダ配下のサブフォルダとファイルをMain画面に表示
    if selected_folder:
        # 現在の表示パス（選択フォルダ直下からナビゲーション）
        # クエリパラメータ path があれば、そのパスへ移動（リンククリック時に反映）
        try:
            _qp = st.query_params
            _q_path = _qp.get("path")
            if isinstance(_q_path, list):
                _q_path = _q_path[0] if _q_path else None
            if _q_path:
                sel = (selected_folder or "").rstrip('/')
                if _q_path == sel or _q_path.startswith(sel + "/"):
                    st.session_state.current_folder = _q_path
                else:
                    # 選択外のpathは無視し、削除
                    try:
                        del _qp['path']
                    except Exception:
                        pass
        except Exception:
            pass

        current_path = st.session_state.current_folder or selected_folder
        # 最終的な表示パスが確定してから一度だけ未インデックス警告を評価
        _ensure_index_warning(current_path)
        # 以前の見た目を保ちつつ、各区切りをインラインリンク化
        parts = [p for p in (current_path or "/").strip('/').split('/') if p]
        acc = ""
        links = []
        for p in parts:
            acc = f"{acc}/{p}" if acc else f"/{p}"
            href = f"?path={quote(acc, safe='/')}"
            links.append(f'<a href="{href}" style="text-decoration:none;">{p}</a>')
        display = "/" if not links else " / ".join(links)
        st.markdown(f"###### 📂 現在のフォルダ: {display}", unsafe_allow_html=True)

        # 親フォルダへ戻る（選択フォルダより上には戻らない）
        if current_path and current_path != selected_folder:
            parent_path = os.path.dirname(current_path.rstrip('/'))
            if not parent_path or not parent_path.startswith(selected_folder):
                parent_path = selected_folder
            if st.button("⬆️ 親フォルダへ"):
                st.session_state.current_folder = parent_path
                st.session_state.filtered_files = None
                st.rerun()

        # 常に表示: サブフォルダのファイルも表示（状態保持）
        st.checkbox(
            "サブフォルダのファイルも表示",
            value=st.session_state.get("show_recursive", False),
            key="show_recursive",
        )

        # 絞り込まれたファイルリストがある場合はそれを使用、なければ全ファイルを表示
        if st.session_state.filtered_files is not None:
            files = st.session_state.filtered_files
            st.markdown(f"##### 📄 ファイル（絞り込み結果）")
            st.info(f"🔍 検索結果: {len(files)}件のファイルが表示されています")
        else:
            # サブフォルダ表示（左に「対象に含める」チェックボックス）
            subfolders = get_subfolders(current_path)
            if subfolders:
                st.markdown("##### 📁 サブフォルダ")
                for i, folder in enumerate(subfolders):
                    cols = st.columns([1, 8, 3])
                    with cols[0]:
                        key_cb = f"include_cb_{folder['full_path']}"
                        checked = folder['full_path'] in st.session_state.included_folders
                        new_val = st.checkbox("", value=checked, key=key_cb, help="チェックするとこのフォルダ配下のみを対象に含めます")
                        # checkboxはkeyで状態保持されるため、集合も同期
                        if new_val:
                            st.session_state.included_folders.add(folder['full_path'])
                        else:
                            st.session_state.included_folders.discard(folder['full_path'])
                    with cols[1]:
                        if st.button(f"📁 {folder['name']}", key=f"subfolder_{folder['full_path']}"):
                            st.session_state.current_folder = folder['full_path']
                            st.session_state.filtered_files = None
                            _ensure_index_warning(st.session_state.current_folder)
                            st.rerun()
                    with cols[2]:
                        is_selected = folder['full_path'] in st.session_state.included_folders
                        is_indexed = False
                        try:
                            is_indexed = count_indexed_files_in(folder['full_path']) > 0
                        except Exception:
                            is_indexed = False
                        if is_selected or is_indexed:
                            st.caption("対象")
            
            # ファイル表示
            st.markdown(f"##### 📄 ファイル")
            # 含める対象のみを反映してファイル一覧を取得
            if st.session_state.get("show_recursive"):
                files_all = _list_files_recursive_cached(current_path)
            else:
                files_all = get_files_in_folder(current_path)
            def _is_included(p: str) -> bool:
                incs = st.session_state.included_folders
                if not incs:
                    return True  # 未選択なら全て対象
                p = (p or "").rstrip('/')
                for inc in incs:
                    incn = (inc or "").rstrip('/')
                    if p == incn or p.startswith(incn + "/"):
                        return True
                return False
            files = [f for f in (files_all or []) if _is_included(f.get('path') or '')]
        
        if files:
            st.write(f"ファイル数: {len(files)}個")
            
            # ファイル一覧をテーブル形式で表示
            for i, file in enumerate(files):
                col1, col2, col3 = st.columns([10, 2, 2])
                
                with col1:
                    # ファイル名をクリック可能なボタンに変更
                    if st.button(f"📄 {file['name']}", key=f"file_{i}", help="クリックしてファイル内容を表示"):
                        st.session_state.selected_file = file
                        file_content = download_file_content(file['path'])
                        if file_content:
                            file_ext = os.path.splitext(file['name'])[-1].lower()

                            # すべてテキストプレビュー（PDFも先頭5000文字）
                            text = extract_text_simple(file_content, file['name'])
                            preview_limit = 5000 if file_ext in ('.pdf', '.xls', '.xlsx') else 2000
                            st.session_state.file_content_preview = text[:preview_limit] if text else "ファイルの内容を読み取れませんでした。"
                            st.session_state.file_content_preview_images = None
                            st.session_state.file_content_preview_limit = preview_limit
                    # サブフォルダ表示: 検索ヒットや再帰表示で現在フォルダ配下のサブフォルダにある場合だけ表示
                    try:
                        parent_dir = os.path.dirname(file['path'])
                        base_root = current_path
                        rel = os.path.relpath(parent_dir, base_root)
                        if rel not in (".", ""):
                            st.caption(f"📁 {rel}")
                    except Exception:
                        pass
                
                with col2:
                    # ファイルサイズを表示
                    size_mb = file['size'] / (1024 * 1024)
                    st.write(f"{size_mb:.1f}MB")
                
                with col3:
                    # 更新日を表示（datetimeでない場合も安全に表示）
                    mod = file['modified']
                    try:
                        txt = mod.strftime("%Y-%m-%d") if hasattr(mod, 'strftime') else str(mod)
                    except Exception:
                        txt = str(mod)
                    st.write(txt)
        else:
            if st.session_state.filtered_files is not None:
                st.warning("検索条件に一致するファイルがありません")
            else:
                st.info("このフォルダにはサポートされているファイルがありません")

else:
    st.sidebar.write('🔴接続解除')


# サイドバー: インデックス操作
with st.sidebar.expander("インデックス" , expanded=False):
    # ロック状況を表示
    try:
        target = st.session_state.current_folder or (folder_list[0] if folder_list else ROOT_PATH)
        lock_status = is_index_locked(target)
        if lock_status.get("locked"):
            age = lock_status.get("age_sec", 0.0)
            stale = lock_status.get("stale", False)
            st.warning(f"別のインデックス処理が実行中（経過 {age/60:.1f} 分）")
        # 常にロック解除ボタンを出す
        if st.button("🔓 ロック解除", key="btn_force_unlock_any"):
            if force_release_lock(target):
                st.success("ロックを解除しました。再度インデックス化を実行できます。")
            else:
                st.error("ロック解除に失敗しました。")
    except Exception:
        pass
    if st.button("📚 このフォルダをインデックス化/更新"):
        with st.spinner("インデックスを作成/更新しています..."):
            target = st.session_state.current_folder or (folder_list[0] if folder_list else ROOT_PATH)
            include = sorted(list(st.session_state.get("included_folders", set())))
            before = 0
            try:
                before = count_indexed_files_in(target)
            except Exception:
                before = 0
            # 進捗バー（単一）
            prog = st.progress(0, text="インデックス作成/更新を開始します...")
            status_total = st.empty()
            status = st.empty()
            def _cb2(done: int, total: int, path: str):
                pct = int((done/total)*100) if total else 100
                prog.progress(pct, text=f"{done}/{total} {os.path.basename(path) if path else ''}")
                if done == 0:
                    status_total.write(f"対象ファイル: {total}件")
                if path:
                    status.write(f"処理中: {path}")
            result = build_index(target, include_prefixes=include, progress_cb=_cb2)
            # n-gram（texts_ng）が空のケースを補完
            try:
                backfilled = backfill_texts_ng()
                if backfilled:
                    st.sidebar.info(f"n-gramを{backfilled}件補完しました")
            except Exception:
                pass
        # 結果に応じて表示（実行中は下部に進捗の見方も案内）
        try:
            if not result.get("started"):
                st.warning("別のインデックス処理が実行中のためスキップしました。しばらくしてから再試行してください。")
            else:
                after = count_indexed_files_in(target)
                delta = max(0, after - before)
                st.success(
                    f"インデックス更新が完了しました（新規 {delta} 件 / 追加 {result.get('indexed',0)} 件 / スキップ {result.get('skipped',0)} 件 / {result.get('duration_sec',0):.1f}s）"
                )
                st.caption("実行中の詳細は、ターミナルに [indexer] ログが逐次出力されます（INDEX_DEBUG=1）。")
        except Exception:
            st.success("インデックス更新が完了しました")

    # 追加: 容量表示と全削除
    sizes = get_storage_bytes()
    def _fmt(b):
        return f"{b/1024/1024:.1f} MB"
    st.caption(
        f"容量: SQLite {_fmt(sizes.get('sqlite',0))} / WAL {_fmt(sizes.get('wal',0))} / SHM {_fmt(sizes.get('shm',0))} / Vector {_fmt(sizes.get('vector',0))} / 合計 {_fmt(sizes.get('total',0))}"
    )
    if st.button("🗑 全インデックス削除（サイズ解放）"):
        with st.spinner("インデックスを削除しています..."):
            freed = reset_index()
        st.success(f"削除完了。解放: {_fmt(freed)}")


# サイドバー: 高速検索（インデックス） フォーム送信時のみ実行
st.sidebar.markdown("### 高速検索（インデックス）")
with st.sidebar.form("index_search_form", clear_on_submit=False):
    query = st.text_input("キーワード", value=st.session_state.get("index_query", ""))
    exact_only = st.checkbox(
        "厳密一致（本文を再確認）",
        value=st.session_state.get("index_exact_only", False),
        help="n-gram候補から実際に文字列を含むものだけに限定します",
    )
    use_vector = st.checkbox(
        "ベクター検索（遅い）",
        value=st.session_state.get("index_use_vector", False),
        help="埋め込み取得に外部APIを使うため遅くなる場合があります",
    )
    submitted = st.form_submit_button("🔍 検索")

if submitted:
    if not query:
        st.sidebar.warning("キーワードを入力してください")
    else:
        # 入力値を保持
        st.session_state.index_query = query
        st.session_state.index_exact_only = exact_only
        st.session_state.index_use_vector = use_vector

        # FTS, n-gram FTS, ベクター の3系統を叩いてマージ
        t0 = time.perf_counter()
        current_prefix = (st.session_state.current_folder or (folder_list[0] if folder_list else ROOT_PATH))
        fts_hits = (search_fts(query, limit=50, folder_prefix=current_prefix)
                    if not exact_only else search_fts_ng_exact(query, limit=50, folder_prefix=current_prefix))
        t1 = time.perf_counter()
        ng_hits = [] if exact_only else search_fts_ng(query, limit=50, folder_prefix=current_prefix)
        t2 = time.perf_counter()
        vec_hits = search_vector(query, k=20, folder_prefix=current_prefix) if use_vector else []
        t3 = time.perf_counter()
        # 現在のフォルダ配下に限定して集計
        target_folder = (st.session_state.current_folder or (folder_list[0] if folder_list else ROOT_PATH)).rstrip('/')
        def _in_folder(hit):
            # hit: (id, path)
            p = str(hit[1])
            return p.startswith(target_folder + "/") if target_folder else True
        # 含めるフォルダでフィルタ
        def _is_included_hit(hit):
            p = (str(hit[1]) or '').rstrip('/')
            incs = st.session_state.get('included_folders', set())
            if not incs:
                return True
            for inc in incs:
                incn = (inc or '').rstrip('/')
                if p == incn or p.startswith(incn + '/'):
                    return True
            return False
        fts_hits_f = [h for h in fts_hits if _in_folder(h) and _is_included_hit(h)]
        ng_hits_f = [h for h in ng_hits if _in_folder(h) and _is_included_hit(h)]
        vec_hits_f = [h for h in vec_hits if _in_folder(h) and _is_included_hit(h)]

        merged_ids = []
        for hid in [h[0] for h in fts_hits_f + ng_hits_f + vec_hits_f]:
            if hid not in merged_ids:
                merged_ids.append(hid)
        if merged_ids:
            # メイン画面に結果を表示できるよう、絞り込みリストに反映
            try:
                files_found = get_files_by_ids(merged_ids)
            except Exception:
                files_found = []

            # 既にリストがある場合は、その中からさらに絞り込み
            if st.session_state.get("filtered_files") is not None:
                allowed = {f.get('path') for f in st.session_state.filtered_files or []}
                files_filtered = [f for f in files_found if f.get('path') in allowed]
                st.sidebar.info(f"絞り込みヒット: {len(files_filtered)} 件（候補 {len(files_found)} 件）")
                st.session_state.filtered_files = files_filtered
            else:
                st.sidebar.info(f"インデックス検索ヒット: {len(files_found)} 件")
                st.session_state.filtered_files = files_found

            st.sidebar.caption(
                f"FTS: {len(fts_hits_f)}件 ({(t1-t0)*1000:.0f}ms) / "
                f"n-gram: {len(ng_hits_f)}件 ({(t2-t1)*1000:.0f}ms) / "
                f"Vector: {len(vec_hits_f)}件 ({(t3-t2)*1000:.0f}ms)"
            )
        else:
            st.sidebar.info("インデックスにヒットしませんでした")
            st.session_state.filtered_files = []

        # メインエリアに結果を反映するため即時再描画
        st.rerun()


# 指示ボックス
prompt = st.sidebar.chat_input("指示を出して下さい")

if prompt:
    if not folder_list or not selected_folder:
        st.sidebar.warning("Dropboxに接続し、フォルダを選択してください。")
    else:
        st.session_state.messages.append({"role": "user", "content": prompt})

        # 検索：現在表示中のフォルダを対象に実行（含めるフォルダ反映）
        current_path = (st.session_state.current_folder or selected_folder)
        if st.session_state.filtered_files is None:
            include = sorted(list(st.session_state.get("included_folders", set())))
            results = search_files_comprehensive(current_path, prompt, include)
        else:
            results = search_from_filtered_files(st.session_state.filtered_files, prompt)

        # 結果整形と履歴反映
        if results:
            st.session_state.filtered_files = [result['file'] for result in results]
            response = f"検索結果: {len(results)}件のファイルが見つかりました\n\n"
            for i, result in enumerate(results, 1):
                match_type = "ファイル名" if result['match_type'] == 'filename' else "内容"
                response += f"{i}. {result['file']['name']} ({match_type}でマッチ)\n"
        else:
            response = "該当するファイルが見つかりませんでした"

        st.session_state.messages.append({"role": "assistant", "content": response})


# プレビューエリア（プロンプトの下に配置）
if st.session_state.selected_file:
    display_name = st.session_state.selected_file['name']
    st.markdown(f"##### 📋 ファイル内容プレビュー: {display_name}")

    file_ext = os.path.splitext(display_name)[-1].lower()

    # テキストプレビュー（PDF含む）
    if st.session_state.file_content_preview:
        st.text_area(
            f"ファイル内容（先頭{st.session_state.file_content_preview_limit}文字）",
            value=st.session_state.file_content_preview,
            height=500,
            disabled=True
        )
    else:
        st.info("プレビュー内容がありません。")

    if st.button("❌ プレビューを閉じる"):
        st.session_state.selected_file = None
        st.session_state.file_content_preview = None
        st.session_state.file_content_preview_images = None
        st.rerun()





 

# チャット履歴表示
for message in st.session_state.messages:
    with st.sidebar.chat_message(message["role"]):
        st.sidebar.write(message["content"])

# 検索処理後に画面更新
if prompt:
    st.rerun()


# リセットボタン（サイドバー）
if st.sidebar.button("🔄 リセット"):
    st.session_state.filtered_files = None
    st.session_state.messages = []
    st.session_state.selected_file = None
    st.session_state.file_content_preview = None
    st.session_state.file_content_preview_images = None
    st.rerun()

# サイドバーにテストボタンを追加
# if st.sidebar.button("🤖 OpenAI接続テスト"):
#     test_result = test_openai_connection()
#     st.sidebar.success(f"テスト結果: {test_result}")



