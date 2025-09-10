# /Users/air/dev/dropbox-file-search-streamlit/test_pdf.py

import io
import os
from pathlib import Path
from PIL import Image # 画像処理のためにPIL（Pillow）をインポート

# 必要なライブラリのインポート
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None
    print("PyMuPDF is not installed. Skipping PyMuPDF tests.")

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None
    print("PyPDF2 is not installed. Skipping PyPDF2 tests.")

try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text
except ImportError:
    pdfminer_extract_text = None
    print("pdfminer.six is not installed. Skipping pdfminer.six tests.")

try:
    import pytesseract # OCRライブラリ
except ImportError:
    pytesseract = None
    print("Pytesseract is not installed. Skipping OCR tests.")

# テスト対象のPDFファイルパス
PDF_FILE_NAME = "燃料消費量計算書.pdf"
PDF_FILE_PATH = Path(__file__).parent / PDF_FILE_NAME

def test_pdf_extraction(file_path):
    print(f"--- PDFファイルテスト開始: {file_path} ---")

    if not file_path.exists():
        print(f"エラー: ファイルが見つかりません - {file_path}")
        return

    file_content = file_path.read_bytes()

    # ... PyMuPDF, PyPDF2, pdfminer.six のテストロジック（変更なし） ...

    # 4. pytesseract (OCR) での抽出
    if pytesseract and fitz: # OCRにはPyMuPDFで画像を生成する必要があるため fitz も必要
        print("\n=== pytesseract (OCR) でのテキスト抽出 ===")
        extracted_ocr = ""
        try:
            with fitz.open(stream=file_content, filetype="pdf") as doc:
                for page_num in range(min(doc.page_count, 2)): # 最初の2ページまでをOCR
                    page = doc.load_page(page_num)
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) # 高解像度でレンダリング
                    
                    # PixmapをPIL Imageに変換
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    
                    # OCRを実行 (日本語指定)
                    text_from_image = pytesseract.image_to_string(img, lang='jpn') # 'jpn' は日本語言語パック
                    extracted_ocr += text_from_image + "\n"

            if extracted_ocr.strip(): # 空白文字のみでないかチェック
                print(f"抽出文字数 (OCR): {len(extracted_ocr)}")
                print("--- 抽出内容 (OCR 抜粋) ---")
                print(extracted_ocr[:500] + "..." if len(extracted_ocr) > 500 else extracted_ocr)
            else:
                print("OCR でテキストを抽出できませんでした。")
        except Exception as e:
            print(f"pytesseract (OCR) 抽出エラー: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("\n=== pytesseract または PyMuPDF がインストールされていないため、OCRテストはスキップされました ===")

    print(f"\n--- PDFファイルテスト終了: {file_path} ---")

if __name__ == "__main__":
    test_pdf_extraction(PDF_FILE_PATH)