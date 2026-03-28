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
_cal_env = os.environ.get('CALENDAR_IDS_JSON')
if _cal_env:
    CALENDAR_MAP = json.loads(_cal_env)
else:
    CALENDAR_MAP = {
        '関根': 'r.sekine@hreed.co.jp',
        '柴田': 'k.shibata@hreed.co.jp',
        '荻野': 'm.ogino@hreed.co.jp',
        '市川': 't.ichikawa@hreed.co.jp',
        '片山': 'y.katayama@hreed.co.jp',
    }

# カレンダータイトル → ステージのマッピング
PHASE_MAP = {
    '求人提案':     ('推薦済み',  '進行中'),
    '初回面談':     ('書類選考中','進行中'),
    '面談':         ('書類選考中','進行中'),
    'カジュアル面談':('書類選考中','進行中'),
    '2回目面談':    ('一次面接',  '進行中'),
    '二次面談':     ('一次面接',  '進行中'),
    '3回目面談':    ('二次面接',  '進行中'),
    '三次面談':     ('二次面接',  '進行中'),
    '面接対策':     ('最終面接',  '進行中'),
    '一次面接':     ('一次面接',  '進行中'),
    '二次面接':     ('二次面接',  '進行中'),
    '最終面接':     ('最終面接',  '進行中'),
    '内定':         ('内定',      '内定'),
    '入社':         ('入社',      '入社'),
    '辞退':         ('',          '離脱'),
}

# ステージの優先順位（高いほど進んでいる）
STAGE_PRIORITY = ['推薦済み', '書類選考中', '一次面接', '二次面接', '最終面接', '内定', '入社', '離脱']

def get_phase_priority(phase):
    stage, status = PHASE_MAP.get(phase, ('', ''))
    if status == '離脱':
        return len(STAGE_PRIORITY) - 1  # 辞退は最終状態として最優先
    if stage in STAGE_PRIORITY:
        return STAGE_PRIORITY.index(stage)
    return -1

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
    """
    以下のフォーマットに対応:
    1. 【初回面談】田中様 / 企業名
    2. 【初回面談/WEB】田中様  → /以前がフェーズ
    3. 【BIZ/市川】田中様_カジュアル面談  → _以降がフェーズ
    4. 【面談】田中さん  → さん も可
    """
    t = title.strip()

    # 【】の中身を取得
    bracket = re.search(r'【(.+?)】', t)
    if not bracket:
        return None, None, None
    inner = bracket.group(1).strip()

    # 名前部分（様・さま・さん）を取得
    name_match = re.search(r'】(.+?)(?:様|さま|さん)', t)
    if not name_match:
        return None, None, None
    name = name_match.group(1).strip() + '様'

    # フェーズ判定: 【フェーズ/xxx】 or 【フェーズ】
    # /で分割して先頭部分がPHASE_MAPにあればそれがフェーズ
    phase_candidate = inner.split('/')[0].split('／')[0].strip()
    if phase_candidate in PHASE_MAP:
        # 企業名は名前の後ろの / 以降
        company_match = re.search(r'(?:様|さま|さん)\s*[/／]\s*(.+)$', t)
        company = company_match.group(1).strip() if company_match else ''
        return phase_candidate, name, company

    # フェーズ判定: 【部署/CA】名前様_フェーズ
    phase_suffix = re.search(r'(?:様|さま)\s*[_＿]\s*(.+)$', t)
    if phase_suffix:
        phase_part = phase_suffix.group(1).strip()
        for key in PHASE_MAP:
            if key in phase_part:
                return key, name, ''

    return None, None, None

def get_calendar_events(calendar_id):
    """指定カレンダーからイベントを取得"""
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    time_min = (now - timedelta(days=SYNC_DAYS_PAST)).isoformat()
    time_max = (now + timedelta(days=SYNC_DAYS_FUTURE)).isoformat()

    all_events = []
    page_token = None
    while True:
        params = dict(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime',
            maxResults=2500,
        )
        if page_token:
            params['pageToken'] = page_token
        result = calendar_service.events().list(**params).execute()
        all_events.extend(result.get('items', []))
        page_token = result.get('nextPageToken')
        if not page_token:
            break
    return all_events

def sync():
    sheet = sheets_client.open_by_key(SPREADSHEET_KEY)
    ws    = sheet.worksheet(SHEET_NAME)
    rows  = ws.get_all_values()

    # 既存候補者: 名前 → 行番号
    existing = {}
    # 離脱済み候補者: 同期で上書きしない
    dropped_names = set()
    for i, row in enumerate(rows[1:], start=2):
        if row and row[0]:
            existing[row[0]] = i
            if len(row) > 5 and row[5] == '離脱':
                dropped_names.add(row[0])

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

        # 候補者ごとに最新フェーズ・録画URLを集約
        candidate_map = {}
        for event in events:
            title = event.get('summary', '')
            phase, name, company = parse_event_title(title)
            if not phase or phase not in PHASE_MAP:
                continue
            start      = event.get('start', {})
            event_date = start.get('date') or start.get('dateTime', '')[:10]

            # Google Meet録画URLを添付ファイルから抽出（video/mp4を優先）
            recording_url = ''
            for att in event.get('attachments', []):
                if att.get('mimeType') == 'video/mp4':
                    recording_url = att.get('fileUrl', '')
                    break
            # 添付になければ説明欄のDriveリンクをチェック
            if not recording_url:
                description = event.get('description', '') or ''
                rec_match = re.search(r'https://drive\.google\.com/\S+', description)
                if rec_match:
                    recording_url = rec_match.group(0).rstrip('>')

            current = candidate_map.get(name)
            new_priority = get_phase_priority(phase)
            if current is None:
                candidate_map[name] = {'phase': phase, 'company': company, 'date': event_date, 'recording': recording_url}
            elif new_priority > get_phase_priority(current['phase']):
                candidate_map[name] = {'phase': phase, 'company': company or current['company'], 'date': event_date, 'recording': recording_url or current['recording']}
            elif new_priority == get_phase_priority(current['phase']) and event_date >= current['date']:
                candidate_map[name] = {'phase': phase, 'company': company or current['company'], 'date': event_date, 'recording': recording_url or current['recording']}

        print(f"   候補者 {len(candidate_map)}人分を検出")

        batch_updates = []
        new_rows      = []
        added = updated = 0

        for name, info in candidate_map.items():
            # 手動で離脱済みの候補者は上書きしない
            if name in dropped_names:
                print(f"   ⏭️  スキップ（離脱済み）: {name}")
                continue

            phase        = info['phase']
            stage, status = PHASE_MAP[phase]
            company      = info['company']
            date         = info['date']
            recording    = info.get('recording', '')
            drop_stage   = ''
            drop_reason  = ''

            if status == '離脱':
                drop_stage  = '辞退'
                drop_reason = '辞退'
                stage       = ''

            if name in existing:
                row_num = existing[name]
                if company:
                    batch_updates.append({'range': f'C{row_num}', 'values': [[company]]})
                batch_updates.append({'range': f'D{row_num}', 'values': [[date]]})  # 最終イベント日を更新
                batch_updates.append({'range': f'E{row_num}', 'values': [[stage]]})
                batch_updates.append({'range': f'F{row_num}', 'values': [[status]]})
                if drop_stage:
                    batch_updates.append({'range': f'G{row_num}', 'values': [[drop_stage]]})
                if drop_reason:
                    batch_updates.append({'range': f'H{row_num}', 'values': [[drop_reason]]})
                if recording:
                    batch_updates.append({'range': f'J{row_num}', 'values': [[recording]]})
                updated += 1
                print(f"   ✏️  更新: {name} → {stage or '離脱'}{' 🎥' if recording else ''}")
            else:
                new_rows.append([name, ca_name, company, date, stage, status, drop_stage, drop_reason, '', recording])
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

    # ===== 2週間以上動きがない進行中候補者を自動離脱に =====
    auto_dropped = _auto_drop_inactive(ws, rows)
    print(f"\n✅ 同期完了！ 追加: {total_added}人 / 更新: {total_updated}人 / 自動離脱: {auto_dropped}人")

def _auto_drop_inactive(ws, rows, days=14):
    """最終イベントから指定日数以上経過した進行中候補者を自動離脱にする"""
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).date()
    threshold = today - timedelta(days=days)

    auto_dropped = 0
    batch_updates = []

    for i, row in enumerate(rows[1:], start=2):
        if not row or not row[0]:
            continue
        name   = row[0]
        status = row[5] if len(row) > 5 else ''
        date_str = row[3] if len(row) > 3 else ''  # 推薦日 or 最終イベント日

        # 進行中以外はスキップ
        if status != '進行中':
            continue

        # 日付をパース
        if not date_str:
            continue
        try:
            last_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            try:
                last_date = datetime.strptime(date_str, '%Y/%m/%d').date()
            except ValueError:
                continue

        # 14日以上経過していたら自動離脱
        if last_date <= threshold:
            batch_updates.append({'range': f'F{i}', 'values': [['離脱']]})
            batch_updates.append({'range': f'G{i}', 'values': [['長期未更新']]})
            batch_updates.append({'range': f'H{i}', 'values': [[f'{days}日以上更新なし']]})
            auto_dropped += 1
            print(f"   🕐 自動離脱: {name}（最終更新: {date_str}）")

    if batch_updates:
        ws.batch_update(batch_updates)

    return auto_dropped

if __name__ == '__main__':
    sync()
