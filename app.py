import streamlit as st
import PyPDF2
import io
import openpyxl
import docx
from dropbox_client import test_connection, get_dropbox_folders, get_subfolders, get_files_in_folder
from openai_client import test_openai_connection, process_user_instruction
from file_searcher import search_files_comprehensive, download_file_content, extract_text_simple
from keyword_extractor import extract_keywords

st.title("DropBox ファイル検索システム")

# CSSファイルを読み込み
# with open('style.css') as f:
#     st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)


# DropBox APIでフォルダ取得
folder_list = get_dropbox_folders()  

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
    
    # 選択したフォルダのファイル一覧をMain画面に表示
    if selected_folder:
        st.subheader(f"📂 {selected_folder} 内のファイル")
        
        files = get_files_in_folder(selected_folder)
        
        if files:
            st.write(f"ファイル数: {len(files)}個")
            
            # ファイル一覧をテーブル形式で表示
            for file in files:
                col1, col2, col3 = st.columns([3, 1, 1])
                
                with col1:
                    # ファイル名をクリック可能にする（後で概要表示機能追加予定）
                    st.write(f"📄 **{file['name']}**")
                
                with col2:
                    # ファイルサイズを表示
                    size_mb = file['size'] / (1024 * 1024)
                    st.write(f"{size_mb:.1f}MB")
                
                with col3:
                    # 更新日を表示
                    st.write(file['modified'].strftime("%Y-%m-%d"))
        else:
            st.info("このフォルダにはサポートされているファイルがありません")

else:
    st.sidebar.write('🔴接続解除')



# 指示ボックス
if "messages" not in st.session_state:
    st.session_state.messages = []

prompt = st.sidebar.chat_input("指示を出して下さい")    

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})

    # キーワード抽出
    keywords = extract_keywords(prompt)
    if keywords:
        search_term = max(keywords, key=lambda x: x['relevance'])['keyword']
    else:
        search_term = "検索キーワードなし"

    # 統合検索
    results = search_files_comprehensive(selected_folder, prompt)
    
    if results:
        response = f"検索結果: {len(results)}件のファイルが見つかりました\n\n"
        for i, result in enumerate(results, 1):
            match_type = "ファイル名" if result['match_type'] == 'filename' else "内容"
            response += f"{i}. {result['file']['name']} ({match_type}でマッチ)\n"
    else:
        response = "該当するファイルが見つかりませんでした"
    
    st.session_state.messages.append({"role": "assistant", "content": response})

for message in st.session_state.messages:
    with st.sidebar.chat_message(message["role"]):
        st.sidebar.write(message["content"])


# サイドバーにテストボタンを追加
if st.sidebar.button("🤖 OpenAI接続テスト"):
    test_result = test_openai_connection()
    st.sidebar.success(f"テスト結果: {test_result}")



def extract_text_simple(file_content, filename):
    """簡単なテキスト抽出（PDF, TXT, Excel対応）"""
    try:
        if filename.lower().endswith('.pdf'):
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_content))
            text = ""
            for page in pdf_reader.pages:
                text += page.extract_text() + "\n"
            return text
            
        elif filename.lower().endswith('.txt'):
            return file_content.decode('utf-8', errors='ignore')
            
        elif filename.lower().endswith(('.xlsx', '.xls')):
            workbook = openpyxl.load_workbook(io.BytesIO(file_content))
            text = ""
            for sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                text += f"シート: {sheet_name}\n"
                for row in sheet.iter_rows(values_only=True):
                    row_text = " ".join([str(cell) for cell in row if cell is not None])
                    if row_text.strip():
                        text += row_text + "\n"
            return text

        elif filename.lower().endswith('.docx'):
            doc = docx.Document(io.BytesIO(file_content))
            text = ""
            for paragraph in doc.paragraphs:
                text += paragraph.text + "\n"
            return text
            
        else:
            return ""

    except Exception as e:
        print(f"テキスト抽出エラー: {e}")
        return ""


