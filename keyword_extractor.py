from openai_client import process_user_instruction

def extract_keywords(user_input):
    """自然言語から検索キーワードを抽出"""
    prompt = f"""
    以下のユーザーの指示から、ファイル検索に必要なキーワードを抽出してください。
    
    ユーザー指示: {user_input}
    
    抽出すべきキーワードをカンマ区切りで出力してください。
    例: 手順書, マニュアル, 操作説明

    下記のキーワードは抽出しないでください。
    例: 検索, ファイル
    """
    
    response = process_user_instruction(prompt)
    keywords = response.split(',')
    return [kw.strip() for kw in keywords if kw.strip()]
