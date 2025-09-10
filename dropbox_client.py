import dropbox
import streamlit as st
# from config import DROPBOX_REFRESH_TOKEN, DROPBOX_CLIENT_ID, DROPBOX_CLIENT_SECRET

DROPBOX_CLIENT_ID = st.secrets["DROPBOX_CLIENT_ID"]
DROPBOX_CLIENT_SECRET = st.secrets["DROPBOX_CLIENT_SECRET"]
DROPBOX_REFRESH_TOKEN = st.secrets["DROPBOX_REFRESH_TOKEN"]


def get_dropbox_client():
    """Dropboxクライアントを取得（チームスペースRootに自動切替）"""
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
        app_key=DROPBOX_CLIENT_ID,
        app_secret=DROPBOX_CLIENT_SECRET
    )
    # チームスペース（"三友工業株式会社 Dropbox"）直下にアクセスできるよう、
    # 取得したアカウント情報から root_namespace にパスルートを切り替える
    try:
        account = dbx.users_get_current_account()
        root_info = getattr(account, "root_info", None)
        root_ns_id = getattr(root_info, "root_namespace_id", None)
        if root_ns_id:
            dbx = dbx.with_path_root(dropbox.common.PathRoot.namespace_id(root_ns_id))
    except Exception:
        # 切替に失敗しても従来のルートで継続
        pass
    return dbx

def test_connection():
    """接続テスト"""
    dbx = get_dropbox_client()
    account = dbx.users_get_current_account()
    return account.name.display_name

def get_dropbox_folders(path=""):
    """指定パスのフォルダ一覧を取得"""
    dbx = get_dropbox_client()
    
    try:
        # フォルダ一覧を取得
        result = dbx.files_list_folder(path, include_mounted_folders=True)
        folders = []
        
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata):
                folders.append(entry.path_display)
        
        return folders
    except Exception as e:
        return []


def get_subfolders(path=""):
    """指定パスのサブフォルダ一覧を取得"""
    dbx = get_dropbox_client()
    
    try:
        # フォルダ一覧を取得
        result = dbx.files_list_folder(path, include_mounted_folders=True)
        subfolders = []
        
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FolderMetadata):
                subfolders.append({
                    'name': entry.name,
                    'path': entry.path_display,
                    'full_path': entry.path_display
                })
        
        return subfolders
    except Exception as e:
        return []



def get_files_in_folder(path=""):
    """指定フォルダ内のファイル一覧を取得"""
    dbx = get_dropbox_client()
    
    try:
        result = dbx.files_list_folder(path)
        files = []
        
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                # 対応ファイル形式のみフィルタリング
                file_ext = entry.name.lower().split('.')[-1]
                if file_ext in ['pdf', 'txt', 'docx', 'xlsx', 'xls', 'doc']:
                    files.append({
                        'name': entry.name,
                        'path': entry.path_display,
                        'size': entry.size,
                        'modified': entry.server_modified
                    })
        
        return files
    except Exception as e:
        return []