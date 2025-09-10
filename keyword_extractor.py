from openai_client import process_user_instruction

def extract_keywords(user_input):
    """自然言語から検索キーワードを抽出"""
    prompt = f"""
    ユーザーの指示: {user_input}
    
    この指示を分析してください：
    
    A. 除外記法（「! .xls」「! .pdf」など）
    B. 通常の検索（「手順書」「例.docx」など）
    
    除外記法の場合：
    - 先頭に!マークを付けて抽出
    - 例：「! .xls」→ ! .xls: 100
    
    通常の検索の場合：
    - ファイル名に含まれる可能性のある単語を抽出
    - 例：「手順書」→ 手順書: 95
    
    形式: キーワード: 関連度(0-100)
    """

    response = process_user_instruction(prompt)
    print(f"OpenAI response: {response}")  # デバッグ用
    return parse_keywords_with_relevance(response)

def parse_keywords_with_relevance(response):
    """キーワードと関連度をパース"""
    keywords = []
    lines = response.strip().split('\n')
    
    for line in lines:
        if ':' in line and line.strip():
            try:
                keyword_part = line.split(':', 1)[0].strip()
                relevance_part = line.split(':', 1)[1].strip()
                
                # 除外記法のチェック
                if keyword_part.startswith('! '):
                    # 除外記法の場合
                    exclude_terms = keyword_part[2:].split()
                    for term in exclude_terms:
                        keywords.append({
                            'keyword': f"!{term}",
                            'relevance': int(relevance_part),
                            'type': 'exclude'
                        })
                else:
                    # 通常の検索
                    keywords.append({
                        'keyword': keyword_part,
                        'relevance': int(relevance_part),
                        'type': 'search'
                    })
            except ValueError:
                continue
    
    return keywords