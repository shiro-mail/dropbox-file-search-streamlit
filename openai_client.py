import openai
import streamlit as st
# from config import OPENAI_API_KEY

OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]

# OpenAI クライアントの初期化
client = openai.OpenAI(api_key=OPENAI_API_KEY)

def test_openai_connection():
    """OpenAI接続テスト"""
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "user", "content": "Hello! This is a connection test."}
            ],
            max_tokens=50
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"接続エラー: {str(e)}"

def process_user_instruction(prompt):
    """ユーザーの指示を処理"""
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "あなたはDropBoxファイル検索アシスタントです。ユーザーの指示に日本語で応答してください。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"処理エラー: {str(e)}"