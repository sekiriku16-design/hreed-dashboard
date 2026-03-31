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
SYNC_DAYS_PAST = 31  # 3月1日以降を対象（31日前〜）
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
# 【フェーズ】名前様 形式（CA面談フェーズ）
# 企業名（フェーズ）名前様 形式（選考フェーズ）
PHASE_MAP = {
    # ── 【フェーズ】名前様 形式 ──
    '初回面談':  ('初回面談', '進行中'),
    '求人提案':  ('求人提案', '進行中'),
    '面接対策':  ('面接対策', '進行中'),
    # ── 企業名（フェーズ）名前様 形式 ──
    '書類選考':  ('書類選考', '進行中'),
    # 一次面接（表記ゆれ対応）
    '一次面接':  ('一次面接', '進行中'),
    '1次面接':   ('一次面接', '進行中'),
    '一次選考':  ('一次面接', '進行中'),
    '1次選考':   ('一次面接', '進行中'),
    # 二次面接（表記ゆれ対応）
    '二次面接':  ('二次面接', '進行中'),
    '2次面接':   ('二次面接', '進行中'),
    '二次選考':  ('二次面接', '進行中'),
    '2次選考':   ('二次面接', '進行中'),
    # 三次面接（表記ゆれ対応）
    '三次面接':  ('二次面接', '進行中'),
    '3次面接':   ('二次面接', '進行中'),
    '三次選考':  ('二次面接', '進行中'),
    '3次選考':   ('二次面接', '進行中'),
    # 最終面接（表記ゆれ対応）
    '最終面接':  ('最終面接', '進行中'),
    '最終選考':  ('最終面接', '進行中'),
    '内定':      ('内定',     '内定'),
    '入社':      ('入社',     '入社'),
    '辞退':      ('',         '離脱'),
}

# ステージの優先順位（高いほど進んでいる）
STAGE_PRIORITY = ['初回面談', '求人提案', '面接対策', '書類選考', '一次面接', '二次面接', '最終面接', '内定', '入社']

def get_phase_priority(phase):
    stage, status = PHASE_MAP.get(phase, ('', ''))
    if status == '離脱':
        return len(STAGE_PRIORITY)  # 辞退は最終状態として最優先
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
    フォーマット1（CA面談）: 【初回面談】田中太郎様 / 【求人提案】田中太郎様 / 【面接対策】田中太郎様
    フォーマット2（選考）  : ABC株式会社（一次面接）田中太郎様
    """
    t = title.strip()

    # フォーマット1: 【フェーズ】名前様
    bracket = re.search(r'【(.+?)】', t)
    if bracket:
        inner = bracket.group(1).strip()
        phase = inner.split('/')[0].split('／')[0].strip()
        name_match = re.search(r'】(.+?)(?:様|さま|さん)', t)
        if name_match and phase in PHASE_MAP:
            name = name_match.group(1).strip() + '様'
            return phase, name, ''
        return None, None, None

    # フォーマット2: 企業名（フェーズ）名前様
    paren_match = re.match(r'^(.+?)[（(](.+?)[）)](.+?)(?:様|さま|さん)', t)
    if paren_match:
        company = paren_match.group(1).strip()
        phase   = paren_match.group(2).strip()
        name    = paren_match.group(3).strip() + '様'
        if phase in PHASE_MAP:
            return phase, name, company

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
    _all_candidate_maps = []

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
        _all_candidate_maps.append(candidate_map)

        batch_updates = []
        new_rows      = []
        added = updated = 0

        for name, info in candidate_map.items():
            # 手動で離脱済みの候補者は上書きしない（ただし録画URLは更新する）
            if name in dropped_names:
                recording = info.get('recording', '')
                if recording and name in existing:
                    row_num = existing[name]
                    ws.update(f'J{row_num}', [[recording]])
                    print(f"   🎥 録画URL更新（離脱済み）: {name}")
                else:
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
    # ※未来の予定がある候補者は除外
    all_future_names = set()
    jst_now = datetime.now(timezone(timedelta(hours=9)))
    today_str = jst_now.strftime('%Y-%m-%d')
    for ca_candidate_map in _all_candidate_maps:
        for name, info in ca_candidate_map.items():
            if info.get('date', '') > today_str:
                all_future_names.add(name)

    auto_dropped = _auto_drop_inactive(ws, rows, exclude_names=all_future_names)
    print(f"\n✅ 同期完了！ 追加: {total_added}人 / 更新: {total_updated}人 / 自動離脱: {auto_dropped}人")

def _auto_drop_inactive(ws, rows, days=14, exclude_names=None):
    """最終イベントから指定日数以上経過した進行中候補者を自動離脱にする（未来の予定がある人は除外）"""
    if exclude_names is None:
        exclude_names = set()
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

        # 未来の予定がある候補者はスキップ
        if name in exclude_names:
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
