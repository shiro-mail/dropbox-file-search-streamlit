from openai_client import process_user_instruction

def extract_keywords(user_input):
    """自然言語から検索キーワードを抽出"""
    prompt = f"""
    以下のユーザーの指示から、ファイル検索に必要なキーワードを抽出してください。
    
    ユーザー指示: {user_input}
    
    抽出すべきキーワードを以下の形式で出力してください：
    キーワード1: 関連度(0-100)
    キーワード2: 関連度(0-100)

    例:
    手順書: 95
    マニュアル: 80
    操作説明: 70

    下記のキーワードは抽出しないでください。
    例: 検索, ファイル

    過去の指示から抽出したキーワードは抽出しないでください。
    （今回の指示に関するキーワードの場合は抽出してください）
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
                keyword, relevance = line.split(':', 1)
                keyword = keyword.strip()
                relevance = int(relevance.strip())
                keywords.append({
                    'keyword': keyword,
                    'relevance': relevance
                })
            except ValueError:
                continue
    
    return keywords