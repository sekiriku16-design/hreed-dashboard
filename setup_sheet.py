import gspread
from google.oauth2.service_account import Credentials
import warnings
warnings.filterwarnings('ignore')

SPREADSHEET_KEY = '1GPNWEtNnZemkrWm0Y4WhJODcBMTTKJbTJsK9Krj64os'
SHEET_NAME = '候補者管理'

creds = Credentials.from_service_account_file(
    '/Users/sekineriku/hreed-ai/credentials.json',
    scopes=['https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive',
            'https://www.googleapis.com/auth/spreadsheets']
)
client = gspread.authorize(creds)
spreadsheet = client.open_by_key(SPREADSHEET_KEY)

ws = spreadsheet.sheet1
ws.update_title(SHEET_NAME)
print(f"「{SHEET_NAME}」シートを準備しました")

# ヘッダー
headers = ['候補者名', '担当CA', '求人先企業', '推薦日', '現在ステージ', 'ステータス', '離脱ステージ', '離脱理由', '備考']
ws.append_row(headers)

# ヘッダー行の書式（太字・背景色）
ws.format('A1:I1', {
    'backgroundColor': {'red': 0.12, 'green': 0.18, 'blue': 0.28},
    'textFormat': {'bold': True, 'foregroundColor': {'red': 0.9, 'green': 0.95, 'blue': 1.0}},
    'horizontalAlignment': 'CENTER'
})

# 列幅を設定
requests = [
    {'updateDimensionProperties': {
        'range': {'sheetId': ws.id, 'dimension': 'COLUMNS', 'startIndex': i, 'endIndex': i+1},
        'properties': {'pixelSize': w},
        'fields': 'pixelSize'
    }}
    for i, w in enumerate([130, 80, 150, 100, 110, 90, 110, 130, 150])
]
spreadsheet.batch_update({'requests': requests})

# 入力規則（ドロップダウン）
STAGES = ['推薦済み', '書類選考中', '一次面接', '二次面接', '最終面接', '内定', '入社']
DROP_STAGES = ['推薦済み', '書類選考中', '初回面談後', '2回目面談後', '3回目面談後', '一次面接', '二次面接', '最終面接', '内定後']
STATUS = ['進行中', '離脱', '内定', '入社']

def dropdown_rule(sheet_id, col_index, values):
    return {
        'setDataValidation': {
            'range': {
                'sheetId': sheet_id,
                'startRowIndex': 1, 'endRowIndex': 200,
                'startColumnIndex': col_index, 'endColumnIndex': col_index + 1
            },
            'rule': {
                'condition': {
                    'type': 'ONE_OF_LIST',
                    'values': [{'userEnteredValue': v} for v in values]
                },
                'showCustomUi': True,
                'strict': False
            }
        }
    }

spreadsheet.batch_update({'requests': [
    dropdown_rule(ws.id, 4, STAGES),      # 現在ステージ（E列）
    dropdown_rule(ws.id, 5, STATUS),      # ステータス（F列）
    dropdown_rule(ws.id, 6, DROP_STAGES), # 離脱ステージ（G列）
]})

# サンプルデータを3件入れる
samples = [
    ['田中 太郎', '関根', '株式会社サンプルA', '2026/03/20', '一次面接', '進行中', '', '', ''],
    ['鈴木 花子', '関根', '株式会社サンプルB', '2026/03/22', '', '離脱', '書類選考中', '条件不一致', ''],
    ['佐藤 一郎', '関根', '株式会社サンプルC', '2026/03/25', '推薦済み', '進行中', '', '', ''],
]
for row in samples:
    ws.append_row(row)

print()
print("✅ セットアップ完了！")
print(f"   シート名: {SHEET_NAME}")
print(f"   ヘッダー: {', '.join(headers)}")
print(f"   サンプル: 3件入力済み")
print()
print("📝 ステージの選択肢（ドロップダウン）:")
print(f"   現在ステージ → {' / '.join(STAGES)}")
print(f"   離脱ステージ → {' / '.join(DROP_STAGES)}")
print(f"   ステータス → {' / '.join(STATUS)}")
print()
print("🌐 ダッシュボードを起動して確認 → python3 candidates_dashboard.py")
