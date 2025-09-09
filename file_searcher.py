import os
import io
import PyPDF2
import docx
import openpyxl
import xlrd # .xlsファイル対応のために追加
from dropbox_client import get_dropbox_client, get_files_in_folder 
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


def search_files_comprehensive(folder_path, user_input):
    """ファイル名と内容の両方で検索"""
    # ファイル名検索
    filename_results = search_files(folder_path, user_input)
    
    # ファイル内容検索
    content_results = search_files_by_content(folder_path, user_input)
    
    # 結果を統合（重複除去）
    all_results = filename_results + content_results
    unique_results = []
    seen_files = set()
    
    for result in all_results:
        file_path = result['file']['path']
        if file_path not in seen_files:
            unique_results.append(result)
            seen_files.add(file_path)
    
    return unique_results


def search_files_by_content(folder_path, user_input):
    """ファイル内容で検索"""
    # キーワード抽出（関連度トップのみ）
    keywords = extract_keywords(user_input)
    if not keywords:
        return []
    
    top_keyword = max(keywords, key=lambda x: x['relevance'])
    search_term = top_keyword['keyword']
    
    # ファイル一覧を取得
    files = get_files_in_folder(folder_path)
    
    # ファイル内容で検索
    content_results = []
    for file in files:
        # ファイルをダウンロード
        file_content = download_file_content(file['path'])
        if file_content:
            # テキスト抽出（簡単な実装）
            text = extract_text_simple(file_content, file['name'])
            if search_term.lower() in text.lower():
                content_results.append({
                    'file': file,
                    'match_type': 'content',
                    'search_term': search_term
                })
    
    return content_results

def download_file_content(file_path):
    """ファイルをダウンロード"""
    try:
        dbx = get_dropbox_client()
        _, response = dbx.files_download(file_path)
        return response.content
    except Exception as e:
        print(f"ファイルダウンロードエラー: {e}")
        return None

def extract_text_simple(file_content, filename):
    """ファイルの内容からテキストを抽出 (PDF, TXT, Excel, Word対応)"""
    try:
        text = ""
        _, file_ext =  os.path.splitext(filename)
        file_ext = file_ext.lower() # ← ここを修正

        if file_ext.endswith('.pdf'):
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_content))
            for page in pdf_reader.pages:
                text += page.extract_text() + "\n"
            
        elif file_ext.endswith('.txt'):
            text = file_content.decode('utf-8', errors='ignore')
            
        elif file_ext.endswith('.docx'):
            doc = docx.Document(io.BytesIO(file_content))
            for paragraph in doc.paragraphs:
                text += paragraph.text + "\n"
            
        elif file_ext.endswith('.xlsx'):
            workbook = openpyxl.load_workbook(io.BytesIO(file_content))
            for sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                text += f"シート: {sheet_name}\n"
                for row in sheet.iter_rows(values_only=True):
                    row_text = " ".join([str(cell) for cell in row if cell is not None])
                    if row_text.strip():
                        text += row_text + "\n"
            
        elif file_ext.endswith('.xls'):
            workbook = xlrd.open_workbook(file_contents=file_content)
            for sheet_name in workbook.sheet_names():
                sheet = workbook.sheet_by_name(sheet_name)
                text += f"シート: {sheet_name}\n"
                for row_idx in range(sheet.nrows):
                    row_data = []
                    for col_idx in range(sheet.ncols):
                        cell_value = sheet.cell_value(row_idx, col_idx)
                        if cell_value:
                            row_data.append(str(cell_value))
                    if row_data:
                        text += " ".join(row_data) + "\n"
        else:
            print(f"未対応ファイル形式: {filename}")
            return "" # 未対応のファイル形式は空文字列を返す
        
        return text
            
    except Exception as e:
        print(f"テキスト抽出エラー ({filename}): {e}")
        return ""