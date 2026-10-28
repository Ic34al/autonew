import os
import pickle
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

class GdriveUploader:
    def __init__(self):
        self.credentials = None
        self.service = None
        try:
            from bot import Var
            gdrive_upload = getattr(Var, "GDRIVE_UPLOAD", "off")
        except Exception:
            gdrive_upload = "off"
        if gdrive_upload == "on":
            self.load_credentials()
        else:
            self.credentials = None
            self.service = None

    def load_credentials(self):
        creds = None
        token_path = 'token.pickle'
        if os.path.exists(token_path):
            with open(token_path, 'rb') as token_file:
                creds = pickle.load(token_file)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                from bot import Var
                if getattr(Var, "GDRIVE_UPLOAD", "off") == "on":
                    raise Exception("No valid credentials found. Run auth.py first.")
                else:
                    self.credentials = None
                    self.service = None
                    return
        self.credentials = creds
        self.service = build('drive', 'v3', credentials=creds)

    async def upload_file(self, file_path, folder_id=None):
        if not self.service:
            return None, None

        try:
            file_metadata = {'name': os.path.basename(file_path)}
            if folder_id:
                file_metadata['parents'] = [folder_id]

            media = MediaFileUpload(
                file_path,
                resumable=True,
                chunksize=1024 * 1024
            )

            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id,webViewLink'
            ).execute()

            return file.get('id'), file.get('webViewLink')

        except Exception as e:
            print(f"GDrive upload error: {str(e)}")
            return None, None

gdrive = GdriveUploader()
