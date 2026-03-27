import gspread
from google.oauth2.service_account import Credentials
import warnings
warnings.filterwarnings('ignore')
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import urllib.parse
import os
import threading
import time
import json as _json
import base64 as _base64

# ===== 設定 =====
SPREADSHEET_KEY = '1GPNWEtNnZemkrWm0Y4WhJODcBMTTKJbTJsK9Krj64os'
SHEET_NAME = '候補者管理'
STAGES = ['推薦済み', '書類選考中', '一次面接', '二次面接', '最終面接', '内定', '入社']
DROP_STAGES = ['推薦済み', '書類選考中', '初回面談後', '2回目面談後', '3回目面談後', '一次面接', '二次面接', '最終面接', '内定後']
AUTO_SYNC_HOUR_JST = 8  # 毎朝8時に自動同期

# ===== 認証 =====
_CREDS_ENV = os.environ.get('GOOGLE_CREDENTIALS_B64')
if _CREDS_ENV:
    _info = _json.loads(_base64.b64decode(_CREDS_ENV.strip()).decode('utf-8'))
else:
    _creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')
    with open(_creds_path) as f:
        _info = _json.load(f)

creds = Credentials.from_service_account_info(
    _info,
    scopes=['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
)
client = gspread.authorize(creds)

# ===== 最終同期時刻管理 =====
last_sync_time = None

def get_candidates():
    try:
        sheet = client.open_by_key(SPREADSHEET_KEY)
        ws = sheet.worksheet(SHEET_NAME)
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return []
        candidates = []
        for row in rows[1:]:
            if not row[0]:
                continue
            candidates.append({
                'name':        row[0] if len(row) > 0 else '',
                'ca':          row[1] if len(row) > 1 else '',
                'company':     row[2] if len(row) > 2 else '',
                'rec_date':    row[3] if len(row) > 3 else '',
                'stage':       row[4] if len(row) > 4 else '',
                'status':      row[5] if len(row) > 5 else '進行中',
                'drop_stage':  row[6] if len(row) > 6 else '',
                'drop_reason': row[7] if len(row) > 7 else '',
                'memo':        row[8] if len(row) > 8 else '',
            })
        return candidates
    except Exception as e:
        print(f"Sheets接続エラー: {e}")
        return []

def analyze(candidates):
    total = len(candidates)
    active  = [c for c in candidates if c['status'] == '進行中']
    dropped = [c for c in candidates if c['status'] == '離脱']
    offers  = [c for c in candidates if c['status'] in ['内定', '入社']]
    joined  = [c for c in candidates if c['status'] == '入社']

    stage_counts = {s: 0 for s in STAGES}
    for c in active + offers + joined:
        if c['stage'] in stage_counts:
            stage_counts[c['stage']] += 1

    drop_by_stage = {s: 0 for s in DROP_STAGES}
    for c in dropped:
        if c['drop_stage'] in drop_by_stage:
            drop_by_stage[c['drop_stage']] += 1

    drop_reasons = {}
    for c in dropped:
        r = c['drop_reason'] or 'その他'
        drop_reasons[r] = drop_reasons.get(r, 0) + 1

    funnel = []
    for s in STAGES:
        funnel.append({'stage': s, 'active': stage_counts.get(s, 0), 'dropped': drop_by_stage.get(s, 0)})

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

def do_sync():
    global last_sync_time
    try:
        import calendar_sync
        import importlib
        importlib.reload(calendar_sync)
        calendar_sync.sync()
        jst = timezone(timedelta(hours=9))
        last_sync_time = datetime.now(jst).strftime('%Y/%m/%d %H:%M')
        print(f"✅ 自動同期完了: {last_sync_time}")
        return True, None
    except Exception as e:
        print(f"❌ 同期エラー: {e}")
        return False, str(e)

def auto_sync_loop():
    """毎朝8時（JST）に自動同期"""
    jst = timezone(timedelta(hours=9))
    while True:
        now = datetime.now(jst)
        # 次の8時を計算
        next_sync = now.replace(hour=AUTO_SYNC_HOUR_JST, minute=0, second=0, microsecond=0)
        if now >= next_sync:
            next_sync += timedelta(days=1)
        wait_sec = (next_sync - now).total_seconds()
        print(f"⏰ 次回自動同期: {next_sync.strftime('%Y/%m/%d %H:%M')} （{int(wait_sec//3600)}時間後）")
        time.sleep(wait_sec)
        print("🔄 自動同期開始...")
        do_sync()

# 自動同期スレッド起動
sync_thread = threading.Thread(target=auto_sync_loop, daemon=True)
sync_thread.start()

class DashboardHandler(BaseHTTPRequestHandler):
    def _handle_sync(self):
        ok, err = do_sync()
        if ok:
            self.send_response(200)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'synced_at': last_sync_time}, ensure_ascii=False).encode())
        else:
            self.send_response(500)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({'error': err}, ensure_ascii=False).encode())

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/sync':
            self._handle_sync()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == '/sync':
            self._handle_sync()
            return

        if path == '/api/data':
            candidates = get_candidates()
            stats = analyze(candidates)
            jst = timezone(timedelta(hours=9))
            now = datetime.now(jst)
            next_sync = now.replace(hour=AUTO_SYNC_HOUR_JST, minute=0, second=0, microsecond=0)
            if now >= next_sync:
                next_sync += timedelta(days=1)
            ca_list = sorted(set(c['ca'] for c in candidates if c['ca']))
            self.send_response(200)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({
                'stats': stats,
                'candidates': candidates,
                'last_sync': last_sync_time,
                'next_sync': next_sync.strftime('%m/%d %H:%M'),
                'ca_list': ca_list,
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
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'Helvetica Neue',sans-serif; background:#0f172a; color:#e2e8f0; padding:32px; min-height:100vh; }

.header { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:28px; flex-wrap:wrap; gap:12px; }
.header-left h1 { font-size:24px; font-weight:700; }
.header-left .subtitle { color:#64748b; font-size:13px; margin-top:4px; }
.header-right { display:flex; flex-direction:column; align-items:flex-end; gap:6px; }
.sync-info { font-size:12px; color:#475569; text-align:right; }
.sync-info span { color:#38bdf8; }
.btn-row { display:flex; gap:8px; }
.btn { padding:9px 18px; border-radius:8px; font-size:13px; font-weight:700; cursor:pointer; border:none; transition:opacity 0.2s; }
.btn:hover { opacity:0.85; }
.btn-blue  { background:#38bdf8; color:#0f172a; }
.btn-green { background:#34d399; color:#0f172a; }

/* KPI */
.kpi-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px; margin-bottom:24px; }
.kpi { background:#1e293b; border-radius:12px; padding:20px; border-left:4px solid transparent; }
.kpi.blue   { border-color:#38bdf8; }
.kpi.green  { border-color:#34d399; }
.kpi.red    { border-color:#f87171; }
.kpi.gold   { border-color:#fbbf24; }
.kpi.purple { border-color:#a78bfa; }
.kpi-label  { font-size:11px; color:#64748b; margin-bottom:8px; font-weight:600; text-transform:uppercase; letter-spacing:.05em; }
.kpi-value  { font-size:30px; font-weight:700; color:#f1f5f9; }
.kpi-sub    { font-size:11px; color:#475569; margin-top:4px; }

/* Cards */
.section-grid { display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-bottom:20px; }
.card { background:#1e293b; border-radius:12px; padding:22px; }
.card-title { font-size:12px; font-weight:700; color:#64748b; margin-bottom:16px; text-transform:uppercase; letter-spacing:.05em; }

/* Funnel */
.funnel-row { display:flex; align-items:center; margin-bottom:9px; gap:8px; }
.funnel-label { width:80px; font-size:12px; color:#94a3b8; text-align:right; flex-shrink:0; }
.funnel-bar-wrap { flex:1; background:#0f172a; border-radius:5px; height:26px; overflow:hidden; }
.funnel-bar { height:100%; border-radius:5px; display:flex; align-items:center; padding-left:10px; font-size:12px; font-weight:600; color:#fff; transition:width .6s ease; }
.bar-blue   { background:linear-gradient(90deg,#38bdf8,#818cf8); }
.bar-red    { background:linear-gradient(90deg,#f87171,#fb923c); }
.funnel-drop { font-size:11px; color:#f87171; width:44px; flex-shrink:0; text-align:right; }

/* 離脱理由 */
.reason-row { display:flex; justify-content:space-between; align-items:center; padding:7px 0; border-bottom:1px solid #0f172a; }
.reason-row:last-child { border:none; }
.reason-name  { font-size:13px; color:#cbd5e1; }
.reason-count { font-size:13px; font-weight:700; color:#f87171; }

/* テーブル */
.table-wrap { background:#1e293b; border-radius:12px; padding:22px; margin-bottom:20px; overflow-x:auto; }
.filter-row { display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap; }
.filter-btn { background:#0f172a; border:1px solid #334155; color:#94a3b8; padding:5px 13px; border-radius:20px; font-size:12px; cursor:pointer; transition:all .2s; }
.filter-btn:hover,.filter-btn.active { background:#38bdf8; border-color:#38bdf8; color:#0f172a; font-weight:700; }
table { width:100%; border-collapse:collapse; }
th { font-size:11px; color:#475569; text-align:left; padding:8px 12px; border-bottom:1px solid #0f172a; font-weight:600; text-transform:uppercase; letter-spacing:.05em; }
td { font-size:13px; color:#cbd5e1; padding:10px 12px; border-bottom:1px solid #0f172a; }
tr:last-child td { border:none; }
tr:hover td { background:#162032; }
.badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:11px; font-weight:600; }
.badge.進行中 { background:#1e3a5f; color:#38bdf8; }
.badge.離脱   { background:#3b1f1f; color:#f87171; }
.badge.内定   { background:#1f3b2e; color:#34d399; }
.badge.入社   { background:#2d2a1f; color:#fbbf24; }

.empty { color:#475569; font-size:13px; padding:16px 0; }

@media (max-width:768px) {
  .section-grid { grid-template-columns:1fr; }
  body { padding:16px; }
  .header { flex-direction:column; }
}
</style>
</head>
<body>
<div class="header">
  <div class="header-left">
    <h1>📋 Hreed 候補者管理</h1>
    <div class="subtitle" id="update-time">読み込み中...</div>
  </div>
  <div class="header-right">
    <div class="sync-info">最終同期: <span id="last-sync">-</span></div>
    <div class="sync-info">次回自動同期: <span id="next-sync">-</span>（毎朝8時）</div>
    <div class="btn-row">
      <button class="btn btn-blue"  onclick="loadData()">🔄 データ更新</button>
      <button class="btn btn-green" id="sync-btn" onclick="syncCalendar()">📅 カレンダー同期</button>
    </div>
  </div>
</div>

<div class="kpi-grid">
  <div class="kpi blue">  <div class="kpi-label">総候補者数</div><div class="kpi-value" id="kpi-total">-</div></div>
  <div class="kpi green"> <div class="kpi-label">進行中</div>    <div class="kpi-value" id="kpi-active">-</div></div>
  <div class="kpi red">   <div class="kpi-label">離脱</div>      <div class="kpi-value" id="kpi-dropped">-</div><div class="kpi-sub" id="kpi-drop-rate"></div></div>
  <div class="kpi gold">  <div class="kpi-label">内定・入社</div><div class="kpi-value" id="kpi-offers">-</div><div class="kpi-sub" id="kpi-offer-rate"></div></div>
  <div class="kpi purple"><div class="kpi-label">入社確定</div>  <div class="kpi-value" id="kpi-joined">-</div></div>
</div>

<div class="section-grid">
  <div class="card">
    <div class="card-title">📊 ステージ別 進行状況</div>
    <div id="funnel-rows"></div>
  </div>
  <div class="card">
    <div class="card-title">🤝 面談後 離脱タイミング</div>
    <div id="mendan-drops"></div>
    <div class="card-title" style="margin-top:22px">🚪 離脱理由</div>
    <div id="drop-reasons"></div>
  </div>
</div>

<div class="table-wrap">
  <div class="card-title">👥 候補者リスト（直近3ヶ月）</div>
  <div class="filter-row" id="status-filter">
    <button class="filter-btn active" onclick="filterBy('status','all',this)">すべて</button>
    <button class="filter-btn" onclick="filterBy('status','進行中',this)">進行中</button>
    <button class="filter-btn" onclick="filterBy('status','離脱',this)">離脱</button>
    <button class="filter-btn" onclick="filterBy('status','内定',this)">内定</button>
    <button class="filter-btn" onclick="filterBy('status','入社',this)">入社</button>
  </div>
  <div class="filter-row" id="ca-filter" style="margin-top:-4px"></div>
  <table>
    <thead>
      <tr>
        <th>候補者名</th><th>担当CA</th><th>求人先</th><th>推薦日</th><th>現ステージ</th><th>ステータス</th><th>離脱理由</th>
      </tr>
    </thead>
    <tbody id="table-body"></tbody>
  </table>
</div>

<script>
let allCandidates = [];
let currentStatus = 'all';
let currentCA = 'all';

async function loadData() {
  const res = await fetch('/api/data');
  const data = await res.json();
  const { stats, candidates, last_sync, next_sync, ca_list } = data;
  allCandidates = candidates;

  // CA別フィルターボタンを動的生成
  const caFilter = document.getElementById('ca-filter');
  if (ca_list && ca_list.length > 1) {
    caFilter.innerHTML = `
      <span style="font-size:11px;color:#475569;margin-right:4px">CA:</span>
      <button class="filter-btn active" onclick="filterBy('ca','all',this)">全員</button>
      ${ca_list.map(ca => `<button class="filter-btn" onclick="filterBy('ca','${ca}',this)">${ca}</button>`).join('')}
    `;
  } else {
    caFilter.innerHTML = '';
  }

  document.getElementById('update-time').textContent = '最終更新: ' + new Date().toLocaleString('ja-JP');
  document.getElementById('last-sync').textContent = last_sync || '未同期';
  document.getElementById('next-sync').textContent = next_sync || '-';

  // KPI
  document.getElementById('kpi-total').textContent   = stats.total;
  document.getElementById('kpi-active').textContent  = stats.active;
  document.getElementById('kpi-dropped').textContent = stats.dropped;
  document.getElementById('kpi-drop-rate').textContent  = `離脱率 ${stats.drop_rate}%`;
  document.getElementById('kpi-offers').textContent  = stats.offers;
  document.getElementById('kpi-offer-rate').textContent = `内定率 ${stats.offer_rate}%`;
  document.getElementById('kpi-joined').textContent  = stats.joined;

  // ファネル
  const maxActive = Math.max(...stats.funnel.map(f => f.active), 1);
  document.getElementById('funnel-rows').innerHTML = stats.funnel.map(f => `
    <div class="funnel-row">
      <div class="funnel-label">${f.stage}</div>
      <div class="funnel-bar-wrap">
        <div class="funnel-bar bar-blue" style="width:${Math.round(f.active/maxActive*100)}%">
          ${f.active > 0 ? f.active+'人' : ''}
        </div>
      </div>
      <div class="funnel-drop">${f.dropped > 0 ? '−'+f.dropped : ''}</div>
    </div>`).join('');

  // 面談後離脱
  const mendan = stats.mendan_drops;
  const mendanTotal = Object.values(mendan).reduce((a,b)=>a+b,0);
  document.getElementById('mendan-drops').innerHTML = mendanTotal > 0
    ? Object.entries(mendan).map(([label,cnt]) => `
        <div class="funnel-row">
          <div class="funnel-label">${label}</div>
          <div class="funnel-bar-wrap">
            <div class="funnel-bar bar-red" style="width:${cnt>0?Math.max(Math.round(cnt/mendanTotal*100),8):0}%">
              ${cnt > 0 ? cnt+'人' : ''}
            </div>
          </div>
          <div class="funnel-drop" style="color:#94a3b8">${cnt>0?Math.round(cnt/mendanTotal*100)+'%':''}</div>
        </div>`).join('')
    : '<div class="empty">面談後離脱データなし</div>';

  // 離脱理由
  const sorted = Object.entries(stats.drop_reasons).sort((a,b)=>b[1]-a[1]);
  document.getElementById('drop-reasons').innerHTML = sorted.length > 0
    ? sorted.map(([r,cnt]) => `<div class="reason-row"><span class="reason-name">${r}</span><span class="reason-count">${cnt}件</span></div>`).join('')
    : '<div class="empty">離脱データなし</div>';

  renderTable();
}

function renderTable() {
  let filtered = allCandidates;
  if (currentStatus !== 'all') filtered = filtered.filter(c => c.status === currentStatus);
  if (currentCA !== 'all')     filtered = filtered.filter(c => c.ca === currentCA);
  document.getElementById('table-body').innerHTML = filtered.length > 0
    ? filtered.map(c => `<tr>
        <td>${c.name}</td>
        <td>${c.ca}</td>
        <td>${c.company || '-'}</td>
        <td>${c.rec_date || '-'}</td>
        <td>${c.stage || c.drop_stage || '-'}</td>
        <td><span class="badge ${c.status}">${c.status}</span></td>
        <td>${c.drop_reason || '-'}</td>
      </tr>`).join('')
    : `<tr><td colspan="7" style="color:#475569;text-align:center;padding:24px">データなし</td></tr>`;
}

function filterBy(type, value, btn) {
  const groupId = type === 'status' ? 'status-filter' : 'ca-filter';
  document.querySelectorAll(`#${groupId} .filter-btn`).forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  if (type === 'status') currentStatus = value;
  else currentCA = value;
  renderTable();
}

async function syncCalendar() {
  const btn = document.getElementById('sync-btn');
  btn.textContent = '⏳ 同期中...';
  btn.disabled = true;
  try {
    const res = await fetch('/sync', {method:'POST'});
    const data = await res.json();
    if (data.status === 'ok') {
      btn.textContent = '✅ 完了！';
      setTimeout(() => { btn.textContent = '📅 カレンダー同期'; btn.disabled = false; }, 2000);
      loadData();
    } else {
      btn.textContent = '❌ ' + (data.error || 'エラー');
      setTimeout(() => { btn.textContent = '📅 カレンダー同期'; btn.disabled = false; }, 3000);
    }
  } catch(e) {
    btn.textContent = '❌ 失敗';
    setTimeout(() => { btn.textContent = '📅 カレンダー同期'; btn.disabled = false; }, 3000);
  }
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
