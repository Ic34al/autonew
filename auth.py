import os
import pickle
from google_auth_oauthlib.flow import InstalledAppFlow

def generate_token():
    creds_path = os.getenv('GDRIVE_OAUTH_CREDENTIALS', './1.json')
    token_path = 'token.pickle'
    scopes = ['https://www.googleapis.com/auth/drive.file']

    flow = InstalledAppFlow.from_client_secrets_file(creds_path, scopes)
    creds = flow.run_local_server(port=8001)

    with open(token_path, 'wb') as token_file:
        pickle.dump(creds, token_file)
    print("Token saved to", token_path)

if __name__ == '__main__':
    generate_token()
