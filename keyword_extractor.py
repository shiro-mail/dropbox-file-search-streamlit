from openai_client import process_user_instruction

def extract_keywords(user_input):
    """自然言語から検索キーワードを抽出"""
    prompt = f"""
    ユーザーの指示: {user_input}
    
    この指示からファイル検索に使えるキーワードを抽出してください。
    ファイル名に含まれる可能性のある単語を優先してください。
    
    形式: キーワード: 関連度(0-100)
    
    例:
    例: 95
    docx: 80

    ユーザーの指示が「エクセルのファイルを探して」という意味の場合は「.xls」を抽出してください。
    """

    response = process_user_instruction(prompt)
    return parse_keywords_with_relevance(response)

def parse_keywords_with_relevance(response):
    """キーワードと関連度をパース"""
    keywords = []
    lines = response.strip().split('\n')
    
    for line in lines:
        if ':' in line and line.strip():
            try:
                # 括弧内の文字を除去（例：「サンプル (例)」→「サンプル」）
                keyword_part = line.split(':', 1)[0].strip()
                keyword = keyword_part.split('(')[0].strip()
                
                relevance_part = line.split(':', 1)[1].strip()
                # 括弧内の数字を除去（例：「95 (例)」→「95」）
                relevance = int(relevance_part.split('(')[0].strip())
                
                keywords.append({
                    'keyword': keyword,
                    'relevance': relevance
                })
            except ValueError:
                continue
    
    return keywords