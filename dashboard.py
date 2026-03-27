import gspread
from google.oauth2.service_account import Credentials
import warnings
warnings.filterwarnings('ignore')
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

# 認証
creds = Credentials.from_service_account_file(
    '/Users/sekineriku/hreed-ai/credentials.json',
    scopes=['https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive']
)
client = gspread.authorize(creds)

def get_dashboard_data():
    today = datetime.today()
    col_idx = 20  # デフォルト2026年2月

    sheet = client.open_by_key('1VUVEJ-0puZ_D5F64Wt3Tl5Q0A1IpQThV0hJJwFPK87I')
    worksheet = sheet.worksheet('紹介売上')
    rows = worksheet.get_all_values()

    month_row = rows[8]
    current_month = f"{today.year}年{today.month}月"

    for i, cell in enumerate(month_row):
        if current_month in cell:
            if rows[17][i]:
                col_idx = i
            break

    month_label = month_row[col_idx]
    personal_sales = rows[17][col_idx] if rows[17][col_idx] else '¥0'
    placements = rows[13][col_idx] if rows[13][col_idx] else '0'
    gross_profit = rows[21][col_idx] if rows[21][col_idx] else '¥0'

    # 月別グラフ用データ（直近6ヶ月）
    chart_labels = []
    chart_sales = []
    chart_profit = []

    for i in range(max(2, col_idx - 5), col_idx + 1):
        if i < len(month_row) and month_row[i]:
            label = month_row[i].replace('年', '/').replace('月', '')
            chart_labels.append(label)
            sales_val = rows[17][i] if i < len(rows[17]) and rows[17][i] else '0'
            profit_val = rows[21][i] if i < len(rows[21]) and rows[21][i] else '0'
            sales_num = int(sales_val.replace('¥', '').replace(',', '').replace('0', '0') or 0)
            profit_num = int(profit_val.replace('¥', '').replace(',', '') or 0)
            chart_sales.append(sales_num)
            chart_profit.append(profit_num)

    return {
        'month': month_label,
        'personal_sales': personal_sales,
        'placements': placements,
        'gross_profit': gross_profit,
        'chart_labels': json.dumps(chart_labels),
        'chart_sales': json.dumps(chart_sales),
        'chart_profit': json.dumps(chart_profit),
    }

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = get_dashboard_data()
        html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hreed 経営ダッシュボード</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Helvetica Neue', sans-serif; background: #0f172a; color: #fff; padding: 40px; }}
        h1 {{ font-size: 28px; font-weight: 700; margin-bottom: 8px; }}
        .month {{ color: #94a3b8; font-size: 14px; margin-bottom: 30px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 20px; margin-bottom: 30px; }}
        .card {{ background: #1e293b; border-radius: 16px; padding: 28px; }}
        .card-label {{ font-size: 13px; color: #94a3b8; margin-bottom: 12px; }}
        .card-value {{ font-size: 36px; font-weight: 700; color: #38bdf8; }}
        .card-sub {{ font-size: 13px; color: #64748b; margin-top: 8px; }}
        .chart-card {{ background: #1e293b; border-radius: 16px; padding: 28px; margin-bottom: 30px; }}
        .chart-title {{ font-size: 15px; font-weight: 700; color: #94a3b8; margin-bottom: 20px; }}
        .refresh {{ text-align: center; }}
        button {{ background: #38bdf8; color: #0f172a; border: none; padding: 12px 28px;
                  border-radius: 8px; font-size: 14px; font-weight: 700; cursor: pointer; }}
        button:hover {{ background: #7dd3fc; }}
    </style>
</head>
<body>
    <h1>📊 Hreed 経営ダッシュボード</h1>
    <div class="month">対象月：{data['month']}</div>

    <div class="grid">
        <div class="card">
            <div class="card-label">💰 関根個人 売上</div>
            <div class="card-value">{data['personal_sales']}</div>
            <div class="card-sub">人材紹介合計</div>
        </div>
        <div class="card">
            <div class="card-label">🤝 今月の決定人数</div>
            <div class="card-value">{data['placements']}人</div>
            <div class="card-sub">人材紹介決定数</div>
        </div>
        <div class="card">
            <div class="card-label">📈 粗利益</div>
            <div class="card-value">{data['gross_profit']}</div>
            <div class="card-sub">売上 - 仕入原価</div>
        </div>
    </div>

    <div class="chart-card">
        <div class="chart-title">📊 月別売上・粗利益（直近6ヶ月）</div>
        <canvas id="salesChart" height="100"></canvas>
    </div>

    <div class="refresh">
        <button onclick="location.reload()">🔄 データを更新</button>
    </div>

    <script>
        const labels = {data['chart_labels']};
        const sales = {data['chart_sales']};
        const profit = {data['chart_profit']};

        new Chart(document.getElementById('salesChart'), {{
            type: 'bar',
            data: {{
                labels: labels,
                datasets: [
                    {{
                        label: '売上',
                        data: sales,
                        backgroundColor: 'rgba(56, 189, 248, 0.7)',
                        borderRadius: 6,
                    }},
                    {{
                        label: '粗利益',
                        data: profit,
                        backgroundColor: 'rgba(52, 211, 153, 0.7)',
                        borderRadius: 6,
                    }}
                ]
            }},
            options: {{
                plugins: {{
                    legend: {{ labels: {{ color: '#94a3b8' }} }}
                }},
                scales: {{
                    x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#1e293b' }} }},
                    y: {{
                        ticks: {{
                            color: '#94a3b8',
                            callback: function(v) {{ return '¥' + v.toLocaleString(); }}
                        }},
                        grid: {{ color: '#334155' }}
                    }}
                }}
            }}
        }});
    </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def log_message(self, format, *args):
        pass

print("🚀 ダッシュボード起動中...")
print("ブラウザで → http://localhost:8080 を開いてください！")
server = HTTPServer(('localhost', 8080), DashboardHandler)
server.serve_forever()
