import streamlit as st
from dropbox_client import test_connection, get_dropbox_folders, get_subfolders, get_files_in_folder
from openai_client import test_openai_connection, process_user_instruction


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
    
    # 選択したフォルダのファイル一覧を表示
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

prompt = st.chat_input("指示を出して下さい")
# if prompt:
#     st.session_state.messages.append({"role": "user", "content": prompt})

#     # OpenAI処理に変更
#     response = process_user_instruction(prompt)
#     st.session_state.messages.append({"role": "assistant", "content": response})

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    
    # # キーワード抽出テスト
    # from keyword_extractor import extract_keywords
    # keywords = extract_keywords(prompt)
    
    # # キーワードと関連度を表示
    # keyword_text = ""
    # for kw in keywords:
    #     keyword_text += f"・{kw['keyword']} (関連度: {kw['relevance']}%)\n"

    # response = f"抽出されたキーワード:\n{keyword_text}"

    # ファイル検索
    from file_searcher import search_files
    results = search_files(selected_folder, prompt)
    
    if results:
        response = f"検索結果: {len(results)}件のファイルが見つかりました\n\n"
        for i, result in enumerate(results, 1):
            response += f"{i}. {result['file']['name']}\n"
    else:
        response = "該当するファイルが見つかりませんでした"


    st.session_state.messages.append({"role": "assistant", "content": response})

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])




# サイドバーにテストボタンを追加
if st.sidebar.button("🤖 OpenAI接続テスト"):
    test_result = test_openai_connection()
    st.sidebar.success(f"テスト結果: {test_result}")