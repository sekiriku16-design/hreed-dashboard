import gspread
from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import warnings
warnings.filterwarnings('ignore')
import os
import json
import base64
import re
from datetime import datetime, timedelta, timezone

# ===== 設定 =====
SPREADSHEET_KEY = '1GPNWEtNnZemkrWm0Y4WhJODcBMTTKJbTJsK9Krj64os'
SHEET_NAME = '候補者管理'
CALENDAR_ID = 'r.sekine@hreed.co.jp'  # 関根さんのカレンダーID
SYNC_DAYS_PAST = 90    # 過去何日分を取得するか
SYNC_DAYS_FUTURE = 30  # 未来何日分を取得するか

# カレンダータイトル → ステージのマッピング
PHASE_MAP = {
    '求人提案':  ('推薦済み',    '進行中'),
    '初回面談':  ('書類選考中',  '進行中'),
    '二次面談':  ('一次面接',    '進行中'),
    '三次面談':  ('二次面接',    '進行中'),
    '面接対策':  ('最終面接',    '進行中'),
    '二次面接':  ('二次面接',    '進行中'),
    '最終面接':  ('最終面接',    '進行中'),
    '内定':      ('内定',        '内定'),
    '入社':      ('入社',        '入社'),
    '辞退':      ('',            '離脱'),
}

# ステージの順序（後のフェーズが優先）
STAGE_ORDER = ['推薦済み', '書類選考中', '初回面談後', '2回目面談後', '3回目面談後',
               '一次面接', '二次面接', '最終面接', '内定', '入社']

# ===== 認証 =====
SHEETS_SCOPES = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive',
]

# --- Sheets: サービスアカウント認証 ---
_CREDS_ENV = os.environ.get('GOOGLE_CREDENTIALS_B64')
if _CREDS_ENV:
    _info = json.loads(base64.b64decode(_CREDS_ENV.strip()).decode('utf-8'))
else:
    _creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')
    with open(_creds_path) as f:
        _info = json.load(f)

sheets_creds = Credentials.from_service_account_info(_info, scopes=SHEETS_SCOPES)
sheets_client = gspread.authorize(sheets_creds)

# --- Calendar: OAuth2認証 ---
def _build_calendar_service():
    client_id = os.environ.get('OAUTH_CLIENT_ID')
    client_secret = os.environ.get('OAUTH_CLIENT_SECRET')
    refresh_token = os.environ.get('OAUTH_REFRESH_TOKEN')

    if client_id and client_secret and refresh_token:
        # Railway環境変数から取得
        oauth_creds = OAuthCredentials(
            token=None,
            refresh_token=refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=client_id,
            client_secret=client_secret,
        )
    else:
        # ローカル: oauth_token.json から取得
        token_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'oauth_token.json')
        with open(token_path) as f:
            token_data = json.load(f)
        oauth_creds = OAuthCredentials(
            token=None,
            refresh_token=token_data['refresh_token'],
            token_uri='https://oauth2.googleapis.com/token',
            client_id=token_data['client_id'],
            client_secret=token_data['client_secret'],
        )

    # トークンをリフレッシュ
    oauth_creds.refresh(Request())
    return build('calendar', 'v3', credentials=oauth_creds)

calendar_service = _build_calendar_service()


def parse_event_title(title):
    """【フェーズ】候補者名様 / 企業名 を解析"""
    match = re.match(r'【(.+?)】(.+?)様?\s*(?:/\s*(.+))?$', title.strip())
    if not match:
        return None, None, None
    phase = match.group(1).strip()
    name = match.group(2).strip() + '様'
    name = name.replace('様様', '様')
    company = match.group(3).strip() if match.group(3) else ''
    return phase, name, company

def get_calendar_events():
    """カレンダーからイベントを取得"""
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    time_min = (now - timedelta(days=SYNC_DAYS_PAST)).isoformat()
    time_max = (now + timedelta(days=SYNC_DAYS_FUTURE)).isoformat()

    events_result = calendar_service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime',
        maxResults=500,
    ).execute()
    return events_result.get('items', [])

def sync():
    print("📅 カレンダーからイベントを取得中...")
    events = get_calendar_events()
    print(f"   {len(events)}件のイベントを取得")

    # 候補者ごとに最新フェーズを集約
    candidate_map = {}  # name -> {phase, company, date, event_date}

    for event in events:
        title = event.get('summary', '')
        phase, name, company = parse_event_title(title)
        if not phase or phase not in PHASE_MAP:
            continue

        start = event.get('start', {})
        event_date = start.get('date') or start.get('dateTime', '')[:10]

        # 同じ候補者で複数イベントがある場合、日付が新しいものを優先
        if name not in candidate_map or event_date >= candidate_map[name]['date']:
            candidate_map[name] = {
                'phase': phase,
                'company': company,
                'date': event_date,
            }

    print(f"   候補者 {len(candidate_map)}人分を検出")

    # スプレッドシートを取得
    sheet = sheets_client.open_by_key(SPREADSHEET_KEY)
    ws = sheet.worksheet(SHEET_NAME)
    rows = ws.get_all_values()

    # 既存候補者の名前→行番号マップ
    existing = {}
    for i, row in enumerate(rows[1:], start=2):
        if row and row[0]:
            existing[row[0]] = i

    added = 0
    updated = 0

    for name, info in candidate_map.items():
        phase = info['phase']
        stage, status = PHASE_MAP[phase]
        company = info['company']
        date = info['date']

        # 離脱の場合
        drop_stage = ''
        drop_reason = ''
        if status == '離脱':
            drop_stage = '辞退'
            drop_reason = '辞退'
            stage = ''

        if name in existing:
            # 既存候補者を更新
            row_num = existing[name]
            if company:
                ws.update_cell(row_num, 3, company)
            ws.update_cell(row_num, 5, stage)
            ws.update_cell(row_num, 6, status)
            if drop_stage:
                ws.update_cell(row_num, 7, drop_stage)
            if drop_reason:
                ws.update_cell(row_num, 8, drop_reason)
            updated += 1
            print(f"   ✏️  更新: {name} → {stage or '離脱'}")
        else:
            # 新規候補者を追加
            new_row = [name, '関根', company, date, stage, status, drop_stage, drop_reason, '']
            ws.append_row(new_row)
            added += 1
            print(f"   ➕ 追加: {name} → {stage or '離脱'}")

    print(f"\n✅ 同期完了！ 追加: {added}人 / 更新: {updated}人")

if __name__ == '__main__':
    sync()
