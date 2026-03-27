import gspread
from google.oauth2.service_account import Credentials
import warnings
warnings.filterwarnings('ignore')
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import urllib.parse
import os
import base64
import tempfile

# ===== 設定 =====
SPREADSHEET_KEY = '1GPNWEtNnZemkrWm0Y4WhJODcBMTTKJbTJsK9Krj64os'
SHEET_NAME = '候補者管理'

STAGES = ['推薦済み', '書類選考中', '一次面接', '二次面接', '最終面接', '内定', '入社']
DROP_STAGES = ['推薦済み', '書類選考中', '初回面談後', '2回目面談後', '3回目面談後', '一次面接', '二次面接', '最終面接', '内定後']
# ================================================

# credentials.json をファイルまたは環境変数から読み込む
_CREDS_ENV = os.environ.get('GOOGLE_CREDENTIALS_B64')
if _CREDS_ENV:
    _decoded = base64.b64decode(_CREDS_ENV)
    _tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
    _tmp.write(_decoded)
    _tmp.close()
    _creds_path = _tmp.name
else:
    _creds_path = '/Users/sekineriku/hreed-ai/credentials.json'

creds = Credentials.from_service_account_file(
    _creds_path,
    scopes=['https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive']
)
client = gspread.authorize(creds)

def get_candidates():
    sheet = client.open_by_key(SPREADSHEET_KEY)
    try:
        ws = sheet.worksheet(SHEET_NAME)
    except:
        # シートがなければサンプルデータを返す
        return get_sample_data()

    rows = ws.get_all_values()
    if len(rows) <= 1:
        return get_sample_data()

    candidates = []
    for row in rows[1:]:  # 1行目はヘッダー
        if not row[0]:
            continue
        candidates.append({
            'name':       row[0] if len(row) > 0 else '',
            'ca':         row[1] if len(row) > 1 else '',
            'company':    row[2] if len(row) > 2 else '',
            'rec_date':   row[3] if len(row) > 3 else '',
            'stage':      row[4] if len(row) > 4 else '',
            'status':     row[5] if len(row) > 5 else '進行中',
            'drop_stage': row[6] if len(row) > 6 else '',
            'drop_reason':row[7] if len(row) > 7 else '',
            'memo':       row[8] if len(row) > 8 else '',
        })
    return candidates

def get_sample_data():
    """スプレッドシートが未設定の場合のサンプルデータ"""
    return [
        {'name': '田中 太郎', 'ca': '関根', 'company': '株式会社A', 'rec_date': '2026/03/01', 'stage': '一次面接', 'status': '進行中', 'drop_stage': '', 'drop_reason': '', 'memo': ''},
        {'name': '鈴木 花子', 'ca': '関根', 'company': '株式会社B', 'rec_date': '2026/03/05', 'stage': '書類選考中', 'status': '進行中', 'drop_stage': '', 'drop_reason': '', 'memo': ''},
        {'name': '佐藤 一郎', 'ca': '関根', 'company': '株式会社C', 'rec_date': '2026/03/08', 'stage': '二次面接', 'status': '進行中', 'drop_stage': '', 'drop_reason': '', 'memo': ''},
        {'name': '山田 次郎', 'ca': '関根', 'company': '株式会社A', 'rec_date': '2026/03/10', 'stage': '', 'status': '離脱', 'drop_stage': '書類選考中', 'drop_reason': '条件不一致', 'memo': ''},
        {'name': '高橋 三郎', 'ca': '関根', 'company': '株式会社D', 'rec_date': '2026/03/12', 'stage': '最終面接', 'status': '進行中', 'drop_stage': '', 'drop_reason': '', 'memo': ''},
        {'name': '伊藤 四郎', 'ca': '関根', 'company': '株式会社E', 'rec_date': '2026/03/15', 'stage': '', 'status': '離脱', 'drop_stage': '一次面接', 'drop_reason': '志望度低下', 'memo': ''},
        {'name': '渡辺 五郎', 'ca': '関根', 'company': '株式会社F', 'rec_date': '2026/03/18', 'stage': '内定', 'status': '内定', 'drop_stage': '', 'drop_reason': '', 'memo': ''},
        {'name': '小林 六子', 'ca': '関根', 'company': '株式会社G', 'rec_date': '2026/03/20', 'stage': '推薦済み', 'status': '進行中', 'drop_stage': '', 'drop_reason': '', 'memo': ''},
        {'name': '加藤 七子', 'ca': '関根', 'company': '株式会社H', 'rec_date': '2026/03/22', 'stage': '', 'status': '離脱', 'drop_stage': '最終面接', 'drop_reason': '他社内定', 'memo': ''},
        {'name': '松本 八郎', 'ca': '関根', 'company': '株式会社I', 'rec_date': '2026/03/25', 'stage': '入社', 'status': '入社', 'drop_stage': '', 'drop_reason': '', 'memo': ''},
    ]

def analyze(candidates):
    total = len(candidates)
    active = [c for c in candidates if c['status'] == '進行中']
    dropped = [c for c in candidates if c['status'] == '離脱']
    offers = [c for c in candidates if c['status'] in ['内定', '入社']]
    joined = [c for c in candidates if c['status'] == '入社']

    # ステージ別人数（進行中）
    stage_counts = {s: 0 for s in STAGES}
    for c in active:
        if c['stage'] in stage_counts:
            stage_counts[c['stage']] += 1

    # 内定・入社もカウント
    for c in offers:
        if c['stage'] in stage_counts:
            stage_counts[c['stage']] += 1
    for c in joined:
        if c['stage'] in stage_counts:
            stage_counts[c['stage']] += 1

    # 離脱ステージ別集計（面談後含む）
    drop_by_stage = {s: 0 for s in DROP_STAGES}
    for c in dropped:
        if c['drop_stage'] in drop_by_stage:
            drop_by_stage[c['drop_stage']] += 1

    # 離脱理由集計
    drop_reasons = {}
    for c in dropped:
        r = c['drop_reason'] or 'その他'
        drop_reasons[r] = drop_reasons.get(r, 0) + 1

    # ファネル（進行中ステージ）
    funnel = []
    for s in STAGES:
        passed = stage_counts.get(s, 0)
        dropped_here = drop_by_stage.get(s, 0)
        funnel.append({'stage': s, 'active': passed, 'dropped': dropped_here})

    # 面談後離脱（別セクション）
    mendan_drops = {s: drop_by_stage.get(s, 0) for s in ['初回面談後', '2回目面談後', '3回目面談後']}

    return {
        'total': total,
        'active': len(active),
        'dropped': len(dropped),
        'offers': len(offers),
        'joined': len(joined),
        'drop_rate': round(len(dropped) / total * 100, 1) if total > 0 else 0,
        'offer_rate': round(len(offers) / total * 100, 1) if total > 0 else 0,
        'stage_counts': stage_counts,
        'drop_by_stage': drop_by_stage,
        'drop_reasons': drop_reasons,
        'funnel': funnel,
        'mendan_drops': mendan_drops,
    }

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == '/api/data':
            candidates = get_candidates()
            stats = analyze(candidates)
            self.send_response(200)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({
                'stats': stats,
                'candidates': candidates
            }, ensure_ascii=False).encode('utf-8'))
            return

        html = self.build_html()
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def build_html(self):
        return """<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hreed 候補者管理ダッシュボード</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Helvetica Neue', sans-serif; background: #0f172a; color: #e2e8f0; padding: 32px; min-height: 100vh; }

        h1 { font-size: 26px; font-weight: 700; margin-bottom: 4px; }
        .subtitle { color: #64748b; font-size: 14px; margin-bottom: 28px; }

        /* KPI カード */
        .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 28px; }
        .kpi { background: #1e293b; border-radius: 14px; padding: 22px 24px; border-left: 4px solid transparent; }
        .kpi.blue  { border-color: #38bdf8; }
        .kpi.green { border-color: #34d399; }
        .kpi.red   { border-color: #f87171; }
        .kpi.gold  { border-color: #fbbf24; }
        .kpi.purple{ border-color: #a78bfa; }
        .kpi-label { font-size: 12px; color: #64748b; margin-bottom: 8px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
        .kpi-value { font-size: 32px; font-weight: 700; color: #f1f5f9; }
        .kpi-sub   { font-size: 12px; color: #475569; margin-top: 4px; }

        /* セクション */
        .section-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
        .card { background: #1e293b; border-radius: 14px; padding: 24px; }
        .card-title { font-size: 14px; font-weight: 700; color: #94a3b8; margin-bottom: 18px; text-transform: uppercase; letter-spacing: 0.05em; }

        /* ファネル */
        .funnel-row { display: flex; align-items: center; margin-bottom: 10px; gap: 10px; }
        .funnel-label { width: 90px; font-size: 12px; color: #94a3b8; text-align: right; flex-shrink: 0; }
        .funnel-bar-wrap { flex: 1; background: #0f172a; border-radius: 6px; height: 28px; overflow: hidden; position: relative; }
        .funnel-bar { height: 100%; border-radius: 6px; display: flex; align-items: center; padding-left: 10px; font-size: 12px; font-weight: 600; transition: width 0.6s ease; }
        .funnel-bar.active { background: linear-gradient(90deg, #38bdf8, #818cf8); color: #fff; }
        .funnel-drop { font-size: 11px; color: #f87171; width: 50px; flex-shrink: 0; }
        .funnel-count { font-size: 12px; color: #94a3b8; width: 30px; flex-shrink: 0; text-align: right; }

        /* 離脱理由 */
        .reason-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #0f172a; }
        .reason-row:last-child { border: none; }
        .reason-name { font-size: 13px; color: #cbd5e1; }
        .reason-count { font-size: 13px; font-weight: 700; color: #f87171; }

        /* 候補者テーブル */
        .table-wrap { background: #1e293b; border-radius: 14px; padding: 24px; margin-bottom: 24px; overflow-x: auto; }
        .filter-row { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
        .filter-btn { background: #0f172a; border: 1px solid #334155; color: #94a3b8; padding: 6px 14px; border-radius: 20px; font-size: 12px; cursor: pointer; transition: all 0.2s; }
        .filter-btn:hover, .filter-btn.active { background: #38bdf8; border-color: #38bdf8; color: #0f172a; font-weight: 700; }
        table { width: 100%; border-collapse: collapse; }
        th { font-size: 11px; color: #64748b; text-align: left; padding: 8px 12px; border-bottom: 1px solid #0f172a; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
        td { font-size: 13px; color: #cbd5e1; padding: 10px 12px; border-bottom: 1px solid #1e293b; }
        tr:hover td { background: #0f172a; }
        .badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; }
        .badge.進行中  { background: #1e3a5f; color: #38bdf8; }
        .badge.離脱    { background: #3b1f1f; color: #f87171; }
        .badge.内定    { background: #1f3b2e; color: #34d399; }
        .badge.入社    { background: #2d2a1f; color: #fbbf24; }

        /* リフレッシュ */
        .actions { display: flex; justify-content: flex-end; margin-bottom: 24px; gap: 10px; }
        .btn { padding: 10px 22px; border-radius: 8px; font-size: 13px; font-weight: 700; cursor: pointer; border: none; }
        .btn-primary { background: #38bdf8; color: #0f172a; }
        .btn-primary:hover { background: #7dd3fc; }
        .sample-note { background: #2d1f0e; border: 1px solid #854d0e; border-radius: 10px; padding: 12px 18px; font-size: 12px; color: #fbbf24; margin-bottom: 24px; }

        @media (max-width: 768px) {
            .section-grid { grid-template-columns: 1fr; }
            body { padding: 16px; }
        }
    </style>
</head>
<body>
    <h1>📋 Hreed 候補者管理ダッシュボード</h1>
    <div class="subtitle" id="update-time">読み込み中...</div>

    <div class="sample-note" id="sample-note" style="display:none">
        ⚠️ サンプルデータを表示中です。Googleスプレッドシートに「候補者管理」シートを追加してデータを入力してください。
        <br>ヘッダー行：候補者名 / 担当CA / 求人先企業 / 推薦日 / 現在ステージ / ステータス / 離脱ステージ / 離脱理由 / 備考
    </div>

    <div class="kpi-grid" id="kpi-grid">
        <div class="kpi blue"><div class="kpi-label">総候補者数</div><div class="kpi-value" id="kpi-total">-</div></div>
        <div class="kpi green"><div class="kpi-label">進行中</div><div class="kpi-value" id="kpi-active">-</div></div>
        <div class="kpi red"><div class="kpi-label">離脱数</div><div class="kpi-value" id="kpi-dropped">-</div><div class="kpi-sub" id="kpi-drop-rate"></div></div>
        <div class="kpi gold"><div class="kpi-label">内定・入社</div><div class="kpi-value" id="kpi-offers">-</div><div class="kpi-sub" id="kpi-offer-rate"></div></div>
        <div class="kpi purple"><div class="kpi-label">入社確定</div><div class="kpi-value" id="kpi-joined">-</div></div>
    </div>

    <div class="section-grid">
        <div class="card">
            <div class="card-title">📊 ステージ別ファネル（進行中）</div>
            <div id="funnel-rows"></div>
        </div>
        <div class="card">
            <div class="card-title">🤝 面談後の離脱タイミング</div>
            <div id="mendan-drops"></div>
            <div class="card-title" style="margin-top:24px">🚪 離脱理由の内訳</div>
            <div id="drop-reasons"></div>
        </div>
    </div>

    <div class="table-wrap">
        <div class="card-title">👥 候補者リスト</div>
        <div class="filter-row" id="filter-row">
            <button class="filter-btn active" onclick="filterBy('all')">すべて</button>
            <button class="filter-btn" onclick="filterBy('進行中')">進行中</button>
            <button class="filter-btn" onclick="filterBy('離脱')">離脱</button>
            <button class="filter-btn" onclick="filterBy('内定')">内定</button>
            <button class="filter-btn" onclick="filterBy('入社')">入社</button>
        </div>
        <table id="candidate-table">
            <thead>
                <tr>
                    <th>候補者名</th>
                    <th>担当CA</th>
                    <th>求人先</th>
                    <th>推薦日</th>
                    <th>現ステージ</th>
                    <th>ステータス</th>
                    <th>離脱理由</th>
                </tr>
            </thead>
            <tbody id="table-body"></tbody>
        </table>
    </div>

    <div class="actions">
        <button class="btn btn-primary" onclick="loadData()">🔄 データ更新</button>
    </div>

<script>
let allCandidates = [];
let dropChart = null;

async function loadData() {
    const res = await fetch('/api/data');
    const json = await res.json();
    const { stats, candidates } = json;
    allCandidates = candidates;

    document.getElementById('update-time').textContent =
        '最終更新: ' + new Date().toLocaleString('ja-JP');

    // サンプル判定（最初の候補者が田中太郎ならサンプル）
    const isSample = candidates.length > 0 && candidates[0].name === '田中 太郎';
    document.getElementById('sample-note').style.display = isSample ? 'block' : 'none';

    // KPI
    document.getElementById('kpi-total').textContent = stats.total;
    document.getElementById('kpi-active').textContent = stats.active;
    document.getElementById('kpi-dropped').textContent = stats.dropped;
    document.getElementById('kpi-drop-rate').textContent = `離脱率 ${stats.drop_rate}%`;
    document.getElementById('kpi-offers').textContent = stats.offers;
    document.getElementById('kpi-offer-rate').textContent = `内定率 ${stats.offer_rate}%`;
    document.getElementById('kpi-joined').textContent = stats.joined;

    // ファネル
    const stages = ['推薦済み','書類選考中','一次面接','二次面接','最終面接','内定','入社'];
    const maxActive = Math.max(...stats.funnel.map(f => f.active), 1);
    let funnelHTML = '';
    for (const f of stats.funnel) {
        const pct = Math.round(f.active / maxActive * 100);
        funnelHTML += `
        <div class="funnel-row">
            <div class="funnel-label">${f.stage}</div>
            <div class="funnel-bar-wrap">
                <div class="funnel-bar active" style="width:${pct}%">
                    ${f.active > 0 ? f.active + '人' : ''}
                </div>
            </div>
            <div class="funnel-drop">${f.dropped > 0 ? '−' + f.dropped + '人' : ''}</div>
        </div>`;
    }
    document.getElementById('funnel-rows').innerHTML = funnelHTML;

    // 面談後離脱タイミング
    const mendan = stats.mendan_drops;
    const mendanTotal = Object.values(mendan).reduce((a,b) => a+b, 0);
    let mendanHTML = '';
    for (const [label, cnt] of Object.entries(mendan)) {
        const pct = mendanTotal > 0 ? Math.round(cnt / mendanTotal * 100) : 0;
        mendanHTML += `
        <div class="funnel-row">
            <div class="funnel-label">${label}</div>
            <div class="funnel-bar-wrap">
                <div class="funnel-bar active" style="width:${cnt > 0 ? Math.max(pct, 8) : 0}%; background: linear-gradient(90deg,#f87171,#fb923c)">
                    ${cnt > 0 ? cnt + '人' : ''}
                </div>
            </div>
            <div class="funnel-drop" style="color:#94a3b8">${cnt > 0 ? pct + '%' : ''}</div>
        </div>`;
    }
    document.getElementById('mendan-drops').innerHTML = mendanTotal > 0 ? mendanHTML : '<div style="color:#475569;font-size:13px">面談後離脱データなし</div>';

    // 離脱理由テキスト
    const reasons = stats.drop_reasons;
    let reasonHTML = '';
    const sortedReasons = Object.entries(reasons).sort((a,b) => b[1]-a[1]);
    for (const [r, cnt] of sortedReasons) {
        reasonHTML += `<div class="reason-row"><span class="reason-name">${r}</span><span class="reason-count">${cnt}件</span></div>`;
    }
    document.getElementById('drop-reasons').innerHTML = reasonHTML || '<div style="color:#475569;font-size:13px">離脱データなし</div>';

    // 離脱理由チャート
    if (dropChart) dropChart.destroy();
    if (sortedReasons.length > 0) {
        dropChart = new Chart(document.getElementById('dropChart'), {
            type: 'doughnut',
            data: {
                labels: sortedReasons.map(r => r[0]),
                datasets: [{
                    data: sortedReasons.map(r => r[1]),
                    backgroundColor: ['#f87171','#fbbf24','#a78bfa','#34d399','#38bdf8','#fb923c'],
                    borderWidth: 0,
                }]
            },
            options: {
                plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
                cutout: '65%',
            }
        });
    }

    // テーブル
    renderTable('all');
}

function renderTable(filter) {
    // フィルターボタン更新
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    event && event.target && event.target.classList.add('active');

    const tbody = document.getElementById('table-body');
    const filtered = filter === 'all'
        ? allCandidates
        : allCandidates.filter(c => c.status === filter);

    tbody.innerHTML = filtered.map(c => `
        <tr>
            <td>${c.name}</td>
            <td>${c.ca}</td>
            <td>${c.company}</td>
            <td>${c.rec_date}</td>
            <td>${c.stage || c.drop_stage || '-'}</td>
            <td><span class="badge ${c.status}">${c.status}</span></td>
            <td>${c.drop_reason || '-'}</td>
        </tr>
    `).join('');
}

function filterBy(filter) {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`[onclick="filterBy('${filter}')"]`).classList.add('active');
    renderTable(filter);
}

loadData();
</script>
</body>
</html>"""

    def log_message(self, format, *args):
        pass

PORT = int(os.environ.get('PORT', 8081))
print(f"🚀 候補者管理ダッシュボード起動中... → http://localhost:{PORT}")
server = HTTPServer(('0.0.0.0', PORT), DashboardHandler)
server.serve_forever()
