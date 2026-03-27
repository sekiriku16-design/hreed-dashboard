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
SYNC_DAYS_PAST = 90
SYNC_DAYS_FUTURE = 30

# CA名 → カレンダーID のマッピング
# 環境変数 CALENDAR_IDS_JSON で上書き可能
# 例: {"関根": "r.sekine@hreed.co.jp", "柴田": "s.shibata@hreed.co.jp"}
_cal_env = os.environ.get('CALENDAR_IDS_JSON')
if _cal_env:
    CALENDAR_MAP = json.loads(_cal_env)
else:
    CALENDAR_MAP = {
        '関根': 'r.sekine@hreed.co.jp',
    }

# カレンダータイトル → ステージのマッピング
PHASE_MAP = {
    '求人提案': ('推薦済み',  '進行中'),
    '初回面談': ('書類選考中','進行中'),
    '面談':     ('書類選考中','進行中'),  # 【面談】も初回面談として扱う
    '2回目面談':('一次面接',  '進行中'),
    '二次面談': ('一次面接',  '進行中'),
    '3回目面談':('二次面接',  '進行中'),
    '三次面談': ('二次面接',  '進行中'),
    '面接対策': ('最終面接',  '進行中'),
    '一次面接': ('一次面接',  '進行中'),
    '二次面接': ('二次面接',  '進行中'),
    '最終面接': ('最終面接',  '進行中'),
    '内定':     ('内定',      '内定'),
    '入社':     ('入社',      '入社'),
    '辞退':     ('',          '離脱'),
}

# ===== 認証: Sheets（サービスアカウント）=====
SHEETS_SCOPES = [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive',
]
_CREDS_ENV = os.environ.get('GOOGLE_CREDENTIALS_B64')
if _CREDS_ENV:
    _info = json.loads(base64.b64decode(_CREDS_ENV.strip()).decode('utf-8'))
else:
    _creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')
    with open(_creds_path) as f:
        _info = json.load(f)

sheets_creds = Credentials.from_service_account_info(_info, scopes=SHEETS_SCOPES)
sheets_client = gspread.authorize(sheets_creds)

# ===== 認証: Calendar（OAuth2）=====
def _build_calendar_service():
    client_id     = os.environ.get('OAUTH_CLIENT_ID')
    client_secret = os.environ.get('OAUTH_CLIENT_SECRET')
    refresh_token = os.environ.get('OAUTH_REFRESH_TOKEN')

    if client_id and client_secret and refresh_token:
        oauth_creds = OAuthCredentials(
            token=None,
            refresh_token=refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=client_id,
            client_secret=client_secret,
        )
    else:
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
    oauth_creds.refresh(Request())
    return build('calendar', 'v3', credentials=oauth_creds)

calendar_service = _build_calendar_service()


def parse_event_title(title):
    """【フェーズ】候補者名様 / 企業名 を解析（先頭に番号や記号があってもOK）"""
    # 先頭の番号・記号・スペースをスキップして【】を探す
    match = re.search(r'【(.+?)】(.+?)(?:様|さま|さん)?\s*(?:[/／]\s*(.+))?$', title.strip())
    if not match:
        return None, None, None
    phase   = match.group(1).strip()
    name    = match.group(2).strip()
    # 敬称を統一して「様」に
    if not name.endswith('様'):
        name = name + '様'
    company = match.group(3).strip() if match.group(3) else ''
    return phase, name, company

def get_calendar_events(calendar_id):
    """指定カレンダーからイベントを取得"""
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    time_min = (now - timedelta(days=SYNC_DAYS_PAST)).isoformat()
    time_max = (now + timedelta(days=SYNC_DAYS_FUTURE)).isoformat()

    events_result = calendar_service.events().list(
        calendarId=calendar_id,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime',
        maxResults=500,
    ).execute()
    return events_result.get('items', [])

def sync():
    sheet = sheets_client.open_by_key(SPREADSHEET_KEY)
    ws    = sheet.worksheet(SHEET_NAME)
    rows  = ws.get_all_values()

    # 既存候補者: 名前 → 行番号
    existing = {}
    for i, row in enumerate(rows[1:], start=2):
        if row and row[0]:
            existing[row[0]] = i

    total_added   = 0
    total_updated = 0

    for ca_name, calendar_id in CALENDAR_MAP.items():
        print(f"\n📅 [{ca_name}] カレンダー取得中... ({calendar_id})")
        try:
            events = get_calendar_events(calendar_id)
        except Exception as e:
            print(f"   ⚠️ 取得失敗: {e}")
            continue
        print(f"   {len(events)}件取得")

        # 候補者ごとに最新フェーズを集約
        candidate_map = {}
        for event in events:
            title = event.get('summary', '')
            phase, name, company = parse_event_title(title)
            if not phase or phase not in PHASE_MAP:
                continue
            start      = event.get('start', {})
            event_date = start.get('date') or start.get('dateTime', '')[:10]
            if name not in candidate_map or event_date >= candidate_map[name]['date']:
                candidate_map[name] = {'phase': phase, 'company': company, 'date': event_date}

        print(f"   候補者 {len(candidate_map)}人分を検出")

        batch_updates = []
        new_rows      = []
        added = updated = 0

        for name, info in candidate_map.items():
            phase       = info['phase']
            stage, status = PHASE_MAP[phase]
            company     = info['company']
            date        = info['date']
            drop_stage  = ''
            drop_reason = ''

            if status == '離脱':
                drop_stage  = '辞退'
                drop_reason = '辞退'
                stage       = ''

            if name in existing:
                row_num = existing[name]
                if company:
                    batch_updates.append({'range': f'C{row_num}', 'values': [[company]]})
                batch_updates.append({'range': f'E{row_num}', 'values': [[stage]]})
                batch_updates.append({'range': f'F{row_num}', 'values': [[status]]})
                if drop_stage:
                    batch_updates.append({'range': f'G{row_num}', 'values': [[drop_stage]]})
                if drop_reason:
                    batch_updates.append({'range': f'H{row_num}', 'values': [[drop_reason]]})
                updated += 1
                print(f"   ✏️  更新: {name} → {stage or '離脱'}")
            else:
                new_rows.append([name, ca_name, company, date, stage, status, drop_stage, drop_reason, ''])
                # 既存マップにも追加（同じ名前が他CAにも出た場合の重複防止）
                existing[name] = len(rows) + len(new_rows)
                added += 1
                print(f"   ➕ 追加: {name} → {stage or '離脱'}")

        if batch_updates:
            ws.batch_update(batch_updates)
        if new_rows:
            ws.append_rows(new_rows)

        total_added   += added
        total_updated += updated

    print(f"\n✅ 同期完了！ 追加: {total_added}人 / 更新: {total_updated}人")

if __name__ == '__main__':
    sync()
