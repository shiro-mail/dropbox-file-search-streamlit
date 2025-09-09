import dropbox
from config import DROPBOX_REFRESH_TOKEN, DROPBOX_CLIENT_ID, DROPBOX_CLIENT_SECRET

def get_dropbox_client():
    """Dropboxクライアントを取得"""
    return dropbox.Dropbox(
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
        app_key=DROPBOX_CLIENT_ID,
        app_secret=DROPBOX_CLIENT_SECRET
    )

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
        result = dbx.files_list_folder(path)
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
        result = dbx.files_list_folder(path)
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