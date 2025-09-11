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
from indexer import get_storage_bytes, reset_index


ROOT_PATH = getattr(config, "ROOT_PATH", "")

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# --- 追加: OCRフォールバックを内包したテキスト抽出を使って概要を生成 ---
# extract_text_simple 側でOCRフォールバックが実装されているため、
# ここでは抽出済みテキストをLLMで要約するだけで良い。
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
        try:
            n_indexed = count_indexed_files_in(target_folder)
        except Exception:
            n_indexed = 0
        if n_indexed == 0:
            with st.sidebar:
                st.warning("このフォルダは未インデックスです。作成しますか？")
                if st.button("📚 いま作成する", key=f"btn_build_index_{target_folder}"):
                    with st.spinner("インデックスを作成しています..."):
                        build_index(target_folder)
                        try:
                            backfill_texts_ng()
                        except Exception:
                            pass
                    st.success("インデックス作成が完了しました")

    # フォルダ選択の変更検知と現在フォルダの初期化
    if st.session_state.selected_folder_prev != selected_folder:
        st.session_state.selected_folder_prev = selected_folder
        st.session_state.current_folder = selected_folder
        st.session_state.filtered_files = None
        _ensure_index_warning(st.session_state.current_folder)
    
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
                st.session_state.current_folder = _q_path
                _ensure_index_warning(st.session_state.current_folder)
        except Exception:
            pass

        current_path = st.session_state.current_folder or selected_folder
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
                _ensure_index_warning(st.session_state.current_folder)
                st.rerun()

        # 絞り込まれたファイルリストがある場合はそれを使用、なければ全ファイルを表示
        if st.session_state.filtered_files is not None:
            files = st.session_state.filtered_files
            st.markdown(f"##### 📄 ファイル（絞り込み結果）")
            st.info(f"🔍 検索結果: {len(files)}件のファイルが表示されています")
        else:
            # サブフォルダ表示
            subfolders = get_subfolders(current_path)
            if subfolders:
                st.markdown("##### 📁 サブフォルダ")
                for i, folder in enumerate(subfolders):
                    if st.button(f"📁 {folder['name']}", key=f"subfolder_{folder['full_path']}"):
                        st.session_state.current_folder = folder['full_path']
                        st.session_state.filtered_files = None
                        _ensure_index_warning(st.session_state.current_folder)
                        st.rerun()
            
            # ファイル表示
            files = get_files_in_folder(current_path)
            st.markdown(f"##### 📄 ファイル")
        
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

                            # PDFは画像化してプレビュー
                            if file_ext == '.pdf' and fitz is not None:
                                images = []
                                try:
                                    with fitz.open(stream=file_content, filetype="pdf") as doc:
                                        # 先頭3ページを画像化（必要に応じてページ数を変更）
                                        for page_num in range(min(3, doc.page_count)):
                                            page = doc.load_page(page_num)
                                            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2倍解像度
                                            images.append(pix.pil_tobytes(format="PNG"))
                                    st.session_state.file_content_preview_images = images
                                    st.session_state.file_content_preview = None  # テキストプレビューはクリア
                                except Exception:
                                    st.session_state.file_content_preview = "PDFの画像プレビューを生成できませんでした。"
                                    st.session_state.file_content_preview_images = None

                            # PDF以外（Word/Excel/TXT）は従来通りテキストプレビュー
                            else:
                                text = extract_text_simple(file_content, file['name'])
                                st.session_state.file_content_preview = text[:2000] if text else "ファイルの内容を読み取れませんでした。"
                                st.session_state.file_content_preview_images = None
                    # サブフォルダ表示: 検索ヒットで現在のフォルダ配下のサブフォルダにある場合だけ表示
                    try:
                        parent_dir = os.path.dirname(file['path'])
                        base_root = current_path
                        rel = os.path.relpath(parent_dir, base_root)
                        if st.session_state.filtered_files is not None and rel not in (".", ""):  # 検索結果かつサブフォルダ
                            st.caption(f"📁 {rel}")
                    except Exception:
                        pass
                
                with col2:
                    # ファイルサイズを表示
                    size_mb = file['size'] / (1024 * 1024)
                    st.write(f"{size_mb:.1f}MB")
                
                with col3:
                    # 更新日を表示
                    st.write(file['modified'].strftime("%Y-%m-%d"))
        else:
            if st.session_state.filtered_files is not None:
                st.warning("検索条件に一致するファイルがありません")
            else:
                st.info("このフォルダにはサポートされているファイルがありません")

else:
    st.sidebar.write('🔴接続解除')


# サイドバー: インデックス操作
with st.sidebar.expander("インデックス" , expanded=False):
    if st.button("📚 このフォルダをインデックス化/更新"):
        with st.spinner("インデックスを作成/更新しています..."):
            target = st.session_state.current_folder or (folder_list[0] if folder_list else ROOT_PATH)
            build_index(target)
            # n-gram（texts_ng）が空のケースを補完
            try:
                backfilled = backfill_texts_ng()
                if backfilled:
                    st.sidebar.info(f"n-gramを{backfilled}件補完しました")
            except Exception:
                pass
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


# サイドバー: 高速検索（インデックス）を常時表示
st.sidebar.markdown("### 高速検索（インデックス）")
query = st.sidebar.text_input("キーワード", value="")
exact_only = st.sidebar.checkbox("厳密一致（本文を再確認）", value=False, help="n-gram候補から実際に文字列を含むものだけに限定します")
use_vector = st.sidebar.checkbox("ベクター検索（遅い）", value=False, help="埋め込み取得に外部APIを使うため遅くなる場合があります")
if query:
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
    fts_hits_f = [h for h in fts_hits if _in_folder(h)]
    ng_hits_f = [h for h in ng_hits if _in_folder(h)]
    vec_hits_f = [h for h in vec_hits if _in_folder(h)]

    merged_ids = []
    for hid in [h[0] for h in fts_hits_f + ng_hits_f + vec_hits_f]:
        if hid not in merged_ids:
            merged_ids.append(hid)
    if merged_ids:
        st.sidebar.info(f"インデックス検索ヒット: {len(merged_ids)} 件")
        st.sidebar.caption(
            f"FTS: {len(fts_hits_f)}件 ({(t1-t0)*1000:.0f}ms) / "
            f"n-gram: {len(ng_hits_f)}件 ({(t2-t1)*1000:.0f}ms) / "
            f"Vector: {len(vec_hits_f)}件 ({(t3-t2)*1000:.0f}ms)"
        )
    else:
        st.sidebar.info("インデックスにヒットしませんでした")


# 指示ボックス
prompt = st.sidebar.chat_input("指示を出して下さい")

if prompt:
    if not folder_list or not selected_folder:
        st.sidebar.warning("Dropboxに接続し、フォルダを選択してください。")
    else:
        st.session_state.messages.append({"role": "user", "content": prompt})

        # 検索：現在表示中のフォルダを対象に実行
        current_path = (st.session_state.current_folder or selected_folder)
        if st.session_state.filtered_files is None:
            results = search_files_comprehensive(current_path, prompt)
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

    # PDFは画像（複数ページ）を縦に表示
    if file_ext == '.pdf' and st.session_state.file_content_preview_images:
        for i, img_bytes in enumerate(st.session_state.file_content_preview_images):
            st.image(img_bytes, caption=f"ページ {i+1}", use_container_width=True)

    # それ以外はテキスト
    elif st.session_state.file_content_preview:
        st.text_area(
            "ファイル内容（先頭2000文字）",
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



