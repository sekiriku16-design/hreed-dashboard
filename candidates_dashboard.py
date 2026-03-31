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
STAGES = ['初回面談', '求人提案', '面接対策', '書類選考', '一次面接', '二次面接', '最終面接', '内定', '入社']
AUTO_SYNC_INTERVAL_HOURS = 2  # 2時間おきに自動同期

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
                'recording':   row[9] if len(row) > 9 else '',
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

    # 離脱者の最終ステージで集計（stageカラムが空なら drop_stage を使う）
    drop_by_stage = {s: 0 for s in STAGES}
    drop_by_stage['その他'] = 0
    for c in dropped:
        s = c['stage'] if c['stage'] in drop_by_stage else 'その他'
        drop_by_stage[s] += 1

    drop_reasons = {}
    for c in dropped:
        r = c['drop_reason'] or 'その他'
        drop_reasons[r] = drop_reasons.get(r, 0) + 1

    funnel = []
    for s in STAGES:
        funnel.append({'stage': s, 'active': stage_counts.get(s, 0), 'dropped': drop_by_stage.get(s, 0)})

    # CA面談フェーズ別の離脱内訳
    mendan_drops = {s: drop_by_stage.get(s, 0) for s in ['初回面談', '求人提案', '面接対策']}

    # CA別集計
    ca_names = sorted(set(c['ca'] for c in candidates if c['ca']))
    ca_stats = []
    for ca in ca_names:
        ca_candidates = [c for c in candidates if c['ca'] == ca]
        ca_total   = len(ca_candidates)
        ca_active  = len([c for c in ca_candidates if c['status'] == '進行中'])
        ca_dropped_list = [c for c in ca_candidates if c['status'] == '離脱']
        ca_dropped = len(ca_dropped_list)
        ca_offers  = len([c for c in ca_candidates if c['status'] in ['内定', '入社']])
        ca_drop_detail = {}
        for c in ca_dropped_list:
            ds = c['drop_stage'] or 'その他'
            ca_drop_detail[ds] = ca_drop_detail.get(ds, 0) + 1
        ca_stats.append({
            'ca': ca,
            'total': ca_total,
            'active': ca_active,
            'dropped': ca_dropped,
            'offers': ca_offers,
            'drop_rate': round(ca_dropped / ca_total * 100, 1) if ca_total > 0 else 0,
            'offer_rate': round(ca_offers / ca_total * 100, 1) if ca_total > 0 else 0,
            'drop_detail': ca_drop_detail,
        })

    # ===== ② ステージ転換率 =====
    stage_idx = {s: i for i, s in enumerate(STAGES)}
    def max_stage_idx(c):
        s = c.get('stage', '')
        if s in stage_idx:
            return stage_idx[s]
        return -1

    reached = []
    for i in range(len(STAGES)):
        count = sum(1 for c in candidates if max_stage_idx(c) >= i)
        reached.append(count)

    conversion_rates = []
    for i in range(len(STAGES) - 1):
        from_count = reached[i]
        to_count   = reached[i + 1]
        rate = round(to_count / from_count * 100, 1) if from_count > 0 else 0
        conversion_rates.append({
            'from': STAGES[i],
            'to':   STAGES[i + 1],
            'from_count': from_count,
            'to_count':   to_count,
            'rate': rate,
        })

    # ===== ⑥ 企業別ビュー =====
    company_map = {}
    for c in candidates:
        company = c['company'] or '（未設定）'
        if company not in company_map:
            company_map[company] = []
        company_map[company].append(c)

    company_stats = []
    for company, clist in company_map.items():
        ctotal   = len(clist)
        cactive  = sum(1 for c in clist if c['status'] == '進行中')
        cdropped = sum(1 for c in clist if c['status'] == '離脱')
        coffers  = sum(1 for c in clist if c['status'] in ['内定', '入社'])
        company_stats.append({
            'company':    company,
            'total':      ctotal,
            'active':     cactive,
            'dropped':    cdropped,
            'offers':     coffers,
            'drop_rate':  round(cdropped / ctotal * 100, 1) if ctotal > 0 else 0,
            'offer_rate': round(coffers  / ctotal * 100, 1) if ctotal > 0 else 0,
        })
    company_stats.sort(key=lambda x: x['total'], reverse=True)

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
        'ca_stats': ca_stats,
        'conversion_rates': conversion_rates,
        'company_stats': company_stats,
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
    """2時間おきに自動同期"""
    while True:
        time.sleep(AUTO_SYNC_INTERVAL_HOURS * 3600)
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
            next_sync = now + timedelta(hours=AUTO_SYNC_INTERVAL_HOURS)
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
    <div class="sync-info">次回自動同期: <span id="next-sync">-</span>（2時間おき）</div>
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

<div class="card" style="margin-bottom:20px">
  <div class="card-title">🔀 ステージ転換率</div>
  <div id="conversion-rates" style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-top:4px"></div>
</div>

<div class="card" style="margin-bottom:20px">
  <div class="card-title">👤 CA別 実績</div>
  <div id="ca-stats-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:4px"></div>
</div>

<div class="card" style="margin-bottom:20px">
  <div class="card-title" style="margin-bottom:12px">🏢 企業別 候補者状況</div>
  <div style="margin-bottom:10px">
    <input id="company-search" type="text" placeholder="企業名で絞り込み..." oninput="renderCompanyTable()"
      style="background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:8px;padding:7px 12px;font-size:13px;width:240px;outline:none">
  </div>
  <div style="overflow-x:auto">
    <table id="company-table">
      <thead>
        <tr>
          <th>企業名</th><th>総数</th><th>進行中</th><th>離脱</th><th>離脱率</th><th>内定・入社</th><th>内定率</th>
        </tr>
      </thead>
      <tbody id="company-table-body"></tbody>
    </table>
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
        <th>候補者名</th><th>担当CA</th><th>求人先</th><th>最終更新</th><th>現ステージ</th><th>ステータス</th><th>離脱理由</th><th>録画</th>
      </tr>
    </thead>
    <tbody id="table-body"></tbody>
  </table>
</div>

<script>
let allCandidates = [];
let allCompanyStats = [];
let currentStatus = 'all';
let currentCA = 'all';

async function loadData() {
  const res = await fetch('/api/data');
  const data = await res.json();
  const { stats, candidates, last_sync, next_sync, ca_list } = data;
  allCandidates = candidates;
  allCompanyStats = stats.company_stats || [];

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

  // CA別実績
  const caGrid = document.getElementById('ca-stats-grid');
  if (stats.ca_stats && stats.ca_stats.length > 0) {
    caGrid.innerHTML = stats.ca_stats.map(ca => {
      const dropDetail = Object.entries(ca.drop_detail || {})
        .sort((a,b) => b[1]-a[1])
        .map(([s,n]) => `<span style="font-size:10px;color:#94a3b8">${s}: <b style="color:#f87171">${n}</b></span>`)
        .join('　');
      return `
      <div style="background:#0f172a;border-radius:10px;padding:16px;border-top:3px solid #38bdf8">
        <div style="font-size:13px;font-weight:700;color:#f1f5f9;margin-bottom:12px">👤 ${ca.ca}</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
          <div>
            <div style="font-size:10px;color:#475569;margin-bottom:2px">総候補者</div>
            <div style="font-size:22px;font-weight:700;color:#38bdf8">${ca.total}</div>
          </div>
          <div>
            <div style="font-size:10px;color:#475569;margin-bottom:2px">進行中</div>
            <div style="font-size:22px;font-weight:700;color:#34d399">${ca.active}</div>
          </div>
          <div>
            <div style="font-size:10px;color:#475569;margin-bottom:2px">離脱 (${ca.drop_rate}%)</div>
            <div style="font-size:22px;font-weight:700;color:#f87171">${ca.dropped}</div>
          </div>
          <div>
            <div style="font-size:10px;color:#475569;margin-bottom:2px">内定・入社 (${ca.offer_rate}%)</div>
            <div style="font-size:22px;font-weight:700;color:#fbbf24">${ca.offers}</div>
          </div>
        </div>
        ${dropDetail ? `<div style="padding-top:8px;border-top:1px solid #1e293b;display:flex;flex-wrap:wrap;gap:6px">${dropDetail}</div>` : ''}
      </div>`;
    }).join('');
  } else {
    caGrid.innerHTML = '<div style="color:#475569;font-size:13px">CAデータなし</div>';
  }

  // 転換率
  const cr = stats.conversion_rates || [];
  document.getElementById('conversion-rates').innerHTML = cr.map((r, i) => {
    const color = r.rate >= 70 ? '#34d399' : r.rate >= 40 ? '#fbbf24' : '#f87171';
    return `
      <div style="display:flex;flex-direction:column;align-items:center;background:#0f172a;border-radius:10px;padding:12px 16px;min-width:90px">
        <div style="font-size:11px;color:#64748b;margin-bottom:4px">${r.from}</div>
        <div style="font-size:18px;font-weight:700;color:#f1f5f9">${r.from_count}人</div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:center;gap:2px">
        <div style="font-size:13px;font-weight:700;color:${color}">${r.rate}%</div>
        <div style="font-size:18px;color:#475569">→</div>
      </div>
    `;
  }).join('') + (cr.length > 0 ? `
    <div style="display:flex;flex-direction:column;align-items:center;background:#0f172a;border-radius:10px;padding:12px 16px;min-width:90px">
      <div style="font-size:11px;color:#64748b;margin-bottom:4px">${cr[cr.length-1].to}</div>
      <div style="font-size:18px;font-weight:700;color:#f1f5f9">${cr[cr.length-1].to_count}人</div>
    </div>` : '<div style="color:#475569;font-size:13px">データなし</div>');

  renderCompanyTable();
  renderTable();
}

function renderCompanyTable() {
  const q = (document.getElementById('company-search')?.value || '').trim().toLowerCase();
  const list = q ? allCompanyStats.filter(c => c.company.toLowerCase().includes(q)) : allCompanyStats;
  document.getElementById('company-table-body').innerHTML = list.length > 0
    ? list.map(c => `<tr>
        <td style="font-weight:600;color:#f1f5f9">${c.company}</td>
        <td style="text-align:center">${c.total}</td>
        <td style="text-align:center;color:#34d399">${c.active}</td>
        <td style="text-align:center;color:#f87171">${c.dropped}</td>
        <td style="text-align:center;color:${c.drop_rate>=50?'#f87171':c.drop_rate>=30?'#fbbf24':'#94a3b8'}">${c.drop_rate}%</td>
        <td style="text-align:center;color:#fbbf24">${c.offers}</td>
        <td style="text-align:center;color:${c.offer_rate>=30?'#34d399':c.offer_rate>=10?'#fbbf24':'#94a3b8'}">${c.offer_rate}%</td>
      </tr>`).join('')
    : `<tr><td colspan="7" style="color:#475569;text-align:center;padding:20px">データなし</td></tr>`;
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
        <td>${c.recording ? `<a href="${c.recording}" target="_blank" style="color:#38bdf8;font-size:12px;text-decoration:none">🎥 見る</a>` : '-'}</td>
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
