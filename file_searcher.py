from dropbox_client import get_files_in_folder
from keyword_extractor import extract_keywords

def search_files(folder_path, user_input):
    """指定フォルダ内でファイルを検索"""
    # キーワード抽出（関連度トップのみ）
    keywords = extract_keywords(user_input)
    if not keywords:
        return []
    
    # 関連度トップのキーワードを取得
    top_keyword = max(keywords, key=lambda x: x['relevance'])
    search_term = top_keyword['keyword']
    
    # ファイル一覧を取得
    files = get_files_in_folder(folder_path)
    
    # ファイル名で検索
    search_results = []
    for file in files:
        if search_term.lower() in file['name'].lower():
            search_results.append({
                'file': file,
                'match_type': 'filename',
                'search_term': search_term
            })
    
    return search_results
