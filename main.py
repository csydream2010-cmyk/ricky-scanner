"""
美股市場雷達 V2.0 Ultimate - Pydroid 3 手機旗艦版 (財報修正版)
"""

import pandas as pd
import numpy as np
import time, json, os, schedule, random, warnings, requests
from datetime import datetime
from io import StringIO

warnings.filterwarnings("ignore")

# ==========================================
# 0. 配置區域
# ==========================================
SECTOR_MAP = {
    "科技": "XLK", "通訊": "XLC", "非必需消費": "XLY", "必需消費": "XLP",
    "能源": "XLE", "金融": "XLF", "醫療": "XLV", "工業": "XLI",
    "原物料": "XLB", "房地產": "XLRE", "公用事業": "XLU"
}
STOCK_POOL = {
    "科技": ["AAPL", "MSFT", "NVDA", "AMD", "TSM", "AVGO", "QCOM"],
    "通訊": ["GOOGL", "META", "NFLX", "TMUS"],
    "非必需消費": ["AMZN", "TSLA", "HD", "NKE"],
    "必需消費": ["PG", "KO", "WMT", "COST"],
    "能源": ["XOM", "CVX", "COP"],
    "金融": ["JPM", "BAC", "V", "MA", "GS"],
    "醫療": ["UNH", "JNJ", "LLY", "ABBV"],
    "工業": ["CAT", "UNP", "HON", "GE"],
    "原物料": ["LIN", "APD", "ECL"],
    "房地產": ["PLD", "AMT", "EQIX"],
    "公用事業": ["NEE", "DUK", "SO"]
}

MIN_VOL         = 500_000
MIN_MCAP        = 10_000_000_000
RISK_PER_TRADE  = 0.03
MAX_FILES       = 365
ENABLE_SCHEDULER = False

# ==========================================
# 1. 數據源模組 (手機優化版)
# ==========================================

def make_session():
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    return s

def fetch_vix():
    url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
    try:
        s = make_session()
        r = s.get(url, timeout=15)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        vix = float(df['CLOSE'].iloc[-1])
        print(f"[VIX] CBOE 取得 VIX = {vix}")
        return vix
    except Exception as e:
        print(f"[!] VIX 獲取失敗: {e}")
        return None

def finviz_fetch(ticker, session, retries=3):
    """使用 BeautifulSoup 直接解析 Finviz，相容 pandas 3.x 環境"""
    from bs4 import BeautifulSoup
    url = f"https://finviz.com/quote.ashx?t={ticker}&p=d"
    for attempt in range(retries):
        try:
            time.sleep(random.uniform(2.5, 4.5))
            r = session.get(url, timeout=15)
            if r.status_code == 429:
                time.sleep(60 * (attempt + 1))
                continue
            r.raise_for_status()

            soup = BeautifulSoup(r.text, 'lxml')
            tbl = soup.find('table', {'class': 'snapshot-table2'})
            if not tbl: return None

            tds = [td.get_text(strip=True) for td in tbl.find_all('td')]
            flat = {}
            for i in range(0, len(tds) - 1, 2):
                flat[tds[i]] = tds[i + 1]

            def parse_num(s):
                if not s or s in ('-', 'N/A', 'nan', ''): return None
                # 取第一個空格前的數字（處理 "316.94-3.03%" 或 "316.94 -3.03%" 格式）
                import re
                m = re.match(r'^([\d\.]+)', s.replace(',', ''))
                if not m: return None
                num_str = m.group(1)
                try:
                    if s.endswith('B'): return float(num_str) * 1e9
                    if s.endswith('M'): return float(num_str) * 1e6
                    if s.endswith('K'): return float(num_str) * 1e3
                    return float(num_str)
                except ValueError: return None

            def parse_earnings(s):
                """解析財報日期，支援 'Apr 30 AMC'、'Jul 24 BMO' 等格式"""
                if not s or s in ('-', 'N/A', 'nan', ''): return None
                s = s.split('/')[0].strip()
                parts = s.split(' ')
                if len(parts) < 2: return s
                try:
                    dt = pd.to_datetime(f"{parts[0]} {parts[1]}", format='%b %d')
                    now = datetime.now()
                    candidate = datetime(now.year, dt.month, dt.day)
                    # 若日期已過去超過 30 天，推算為明年
                    if (candidate - now).days < -30:
                        candidate = datetime(now.year + 1, dt.month, dt.day)
                    suffix = ' (盤後)' if 'AMC' in s else (' (盤前)' if 'BMO' in s else '')
                    return candidate.strftime('%Y-%m-%d') + suffix
                except Exception:
                    return s

            return {
                'market_cap':    parse_num(flat.get('Market Cap', '')),
                'avg_volume':    parse_num(flat.get('Avg Volume', '')),
                'w52_high':      parse_num(flat.get('52W High', '')),
                'w52_low':       parse_num(flat.get('52W Low', '')),
                'earnings_date': parse_earnings(flat.get('Earnings', '')),
                'sector':        flat.get('Sector', ''),
                'pe':            parse_num(flat.get('P/E', '')),
            }
        except Exception:
            if attempt < retries - 1: time.sleep(3)
    return None

def fetch_yahoo_direct(ticker, session, retries=3):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {'interval': '1d', 'range': '1y'}
    for attempt in range(retries):
        try:
            time.sleep(random.uniform(0.5, 1.0))
            r = session.get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(30 * (attempt + 1))
                continue
            r.raise_for_status()
            res = r.json()['chart']['result'][0]
            quote = res['indicators']['quote'][0]
            df = pd.DataFrame({
                'Date': pd.to_datetime(res['timestamp'], unit='s'),
                'Open': quote['open'], 'High': quote['high'], 'Low': quote['low'],
                'Close': quote['close'], 'Volume': quote['volume']
            }).dropna().set_index('Date').sort_index()
            return df if len(df) >= 90 else None
        except Exception:
            if attempt < retries - 1: time.sleep(3)
    return None

def yf_batch_download(tickers):
    print(f"  [YF] 開始獲取 {len(tickers)} 檔數據 (API 直連模式)...")
    s = make_session()
    out = {}
    missing = []
    for i, t in enumerate(tickers, 1):
        if i % 10 == 0 or i == len(tickers):
            print(f"  [YF] 進度: {i}/{len(tickers)}...")
        df = fetch_yahoo_direct(t, s)
        if df is not None: out[t] = df
        else: missing.append(t)
    if missing:
        print(f"  [YF] 缺失 {len(missing)} 檔，交給 Stooq 補充: {missing}")
        out.update(stooq_fallback(missing))
    print(f"  [YF] 完成: {len(out)}/{len(tickers)} 檔有效")
    return out

def stooq_fallback(tickers):
    out = {}
    s = make_session()
    for t in tickers:
        try:
            time.sleep(random.uniform(1.0, 2.0))
            url = f"https://stooq.com/q/d/l/?s={t.lower()}.us&i=d"
            r = s.get(url, timeout=15)
            r.raise_for_status()
            df = pd.read_csv(StringIO(r.text.strip()))
            df.columns = [c.strip().capitalize() for c in df.columns]
            df['Date'] = pd.to_datetime(df['Date'])
            df = df.set_index('Date').sort_index().tail(250)
            df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()
            if len(df) >= 90:
                out[t] = df
                print(f"  [Stooq] {t} OK")
        except Exception: continue
    return out

# ==========================================
# 2. 核心分析引擎 (全指標版)
# ==========================================
class MarketEngine:
    def __init__(self):
        self.session = make_session()
        self.vix     = None

    def calculate_indicators(self, df, meta):
        close = float(df['Close'].iloc[-1])
        ma5 = float(df['Close'].rolling(5).mean().iloc[-1])
        ma20 = float(df['Close'].rolling(20).mean().iloc[-1])
        ma50 = float(df['Close'].rolling(50).mean().iloc[-1])
        ma200 = float(df['Close'].rolling(200).mean().iloc[-1]) if len(df) >= 200 else float(df['Close'].mean())
        ema200 = float(df['Close'].ewm(span=200, adjust=False).mean().iloc[-1])
        std = float(df['Close'].rolling(20).std().iloc[-1])
        hl = df['High'] - df['Low']
        hpc = np.abs(df['High'] - df['Close'].shift())
        lpc = np.abs(df['Low'] - df['Close'].shift())
        atr = float(pd.concat([hl, hpc, lpc], axis=1).max(axis=1).rolling(14).mean().iloc[-1])
        bb_up = ma20 + std * 2
        bb_lo = ma20 - std * 2
        kc_up = ma20 + atr * 1.5
        kc_lo = ma20 - atr * 1.5
        squeeze = bool((bb_up < kc_up) and (bb_lo > kc_lo))
        ema12 = df['Close'].ewm(span=12, adjust=False).mean()
        ema26 = df['Close'].ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        macd_sig = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = float((macd_line - macd_sig).iloc[-1])
        delta = df['Close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = float(100 - 100 / (1 + rs.iloc[-1])) if pd.notna(rs.iloc[-1]) else 50.0
        ret_90d = (close / float(df['Close'].iloc[-90])) - 1 if len(df) >= 90 else 0.0
        ret_5d = (close / float(df['Close'].iloc[-5])) - 1 if len(df) >= 5 else 0.0
        ret_ytd = (close / float(df['Close'].iloc[0])) - 1
        stop_loss = close - atr * 2
        risk_amount = (close - stop_loss) / close
        size_pct = min(RISK_PER_TRADE / risk_amount, 0.8) if risk_amount > 0 else 0

        return {
            'close': round(close, 2),
            'open': round(float(df['Open'].iloc[-1]), 2),
            'high': round(float(df['High'].iloc[-1]), 2),
            'low': round(float(df['Low'].iloc[-1]), 2),
            'volume': int(df['Volume'].iloc[-1]),
            'avg_volume_20': int(df['Volume'].rolling(20).mean().iloc[-1]),
            'ma5': round(ma5, 2), 'ma20': round(ma20, 2), 'ma50': round(ma50, 2), 
            'ma200': round(ma200, 2), 'ema200': round(ema200, 2),
            'macd_hist': round(macd_hist, 4), 'rsi_14': round(rsi, 2),
            'squeeze': squeeze, 'atr_14': round(atr, 2),
            'w52_high': round(float(meta.get('w52_high') or df['High'].max()), 2),
            'w52_low': round(float(meta.get('w52_low') or df['Low'].min()), 2),
            'pe': meta.get('pe'),
            'entry': round(close, 2), 'stop': round(stop_loss, 2),
            'target': round(close + atr * 4, 2),
            'ret_90d': round(ret_90d * 100, 2), 'ret_5d': round(ret_5d * 100, 2), 'ret_ytd': round(ret_ytd * 100, 2),
            'size_pct': round(size_pct, 4)
        }

    def run_scanner(self):
        self.vix = fetch_vix()
        etf_tickers = list(SECTOR_MAP.values())
        etf_ohlcv = yf_batch_download(etf_tickers)
        etf_results = {}
        for name, ticker in SECTOR_MAP.items():
            if ticker in etf_ohlcv:
                etf_results[name] = self.calculate_indicators(etf_ohlcv[ticker], {})
        all_rets = [v['ret_90d'] for v in etf_results.values()]
        if all_rets:
            for v in etf_results.values():
                v['rs_rank'] = round(sum(1 for r in all_rets if v['ret_90d'] >= r) / len(all_rets) * 100, 1)
        all_stocks = []
        for stocks in STOCK_POOL.values(): all_stocks.extend(stocks)
        stock_ohlcv = yf_batch_download(all_stocks)
        print(f"\n  [Finviz] 開始獲取 {len(stock_ohlcv)} 檔基本面數據...")
        stock_results = {}
        processed_count = 0
        for sector, stocks in STOCK_POOL.items():
            for t in stocks:
                if t in stock_ohlcv:
                    processed_count += 1
                    print(f"  [{processed_count}/{len(stock_ohlcv)}] 正在處理 {t}...", end="\r")
                    meta = finviz_fetch(t, self.session)
                    if not meta: meta = {}
                    mcap = meta.get('market_cap', 0) or 0
                    if mcap < MIN_MCAP and mcap != 0: continue
                    stats = self.calculate_indicators(stock_ohlcv[t], meta)
                    stats['sector'] = sector
                    stats['earnings_date'] = meta.get('earnings_date', 'N/A')
                    stock_results[t] = stats
        print(f"\n  [Finviz] 完成。")
        stock_rets = [v['ret_90d'] for v in stock_results.values()]
        if stock_rets:
            for v in stock_results.values():
                v['rs_rank'] = round(sum(1 for r in stock_rets if v['ret_90d'] >= r) / len(stock_rets) * 100, 1)
        return {"etfs": etf_results, "stocks": stock_results, "vix": self.vix}

# ==========================================
# 3. 輸出與歸檔模組
# ==========================================

def save_to_archive(data):
    archive_dir = "data_archive"
    os.makedirs(archive_dir, exist_ok=True)
    files = sorted([os.path.join(archive_dir, f) for f in os.listdir(archive_dir) if f.endswith(".json")], key=os.path.getmtime)
    if len(files) >= MAX_FILES: os.remove(files[0])
    path = f"{archive_dir}/market_data_{datetime.now().strftime('%Y-%m-%d')}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    print(f">>> 數據已歸檔: {path}")

def generate_ai_payload(results):
    payload = {
        "scan_metadata": {"scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "vix": results.get("vix")},
        "sector_etfs": [], "individual_stocks": []
    }
    for name, data in results["etfs"].items():
        d = data.copy(); d['sector'] = name; d['ticker'] = SECTOR_MAP[name]
        payload["sector_etfs"].append(d)
    for ticker, data in results["stocks"].items():
        d = data.copy(); d['ticker'] = ticker
        payload["individual_stocks"].append(d)
    with open("market_payload.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(">>> AI 情報包已生成: market_payload.json")

def generate_html(data):
    vix = data.get("vix")
    vix_color = "#f44336" if vix and vix > 25 else ("#ffa726" if vix and vix > 18 else "#4caf50")
    vix_label = f"VIX <span style='color:{vix_color}'>{vix:.2f}</span>" if vix else "VIX N/A"
    html_parts = [
        "<!DOCTYPE html><html lang='zh-Hant'><head><meta charset='UTF-8'><title>交易戰情室 V2.0 Ultimate</title>",
        "<style>body{font-family:'Segoe UI',Arial,sans-serif;background:#0d1117;color:#e6edf3;padding:20px;} table{width:100%;border-collapse:collapse;margin:20px 0;background:#161b22;border-radius:8px;overflow:hidden;} th,td{padding:12px 15px;text-align:left;border-bottom:1px solid #30363d;font-size:13px;} th{background:#21262d;color:#8b949e;text-transform:uppercase;letter-spacing:1px;} tr:hover{background:#1c2128;} .bullish{color:#4caf50;font-weight:bold;} .bearish{color:#f44336;} .warning{color:#ffa726;} .badge{padding:2px 6px;border-radius:4px;font-size:11px;background:#30363d;}</style></head><body>",
        f"<h1>交易戰情室 <small style='font-size:14px;color:#8b949e'>V2.0 Ultimate 手機旗艦版</small></h1>",
        f"<p style='color:#8b949e'>掃描時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {vix_label}</p>"
    ]
    html_parts.append("<h2>🏆 板塊 ETF 強度排名</h2><table><tr><th>板塊</th><th>代碼</th><th>RS Rank</th><th>RSI</th><th>90D %</th><th>YTD %</th><th>Squeeze</th></tr>")
    for name, v in sorted(data["etfs"].items(), key=lambda x: x[1].get('rs_rank', 0), reverse=True):
        html_parts.append(f"<tr><td>{name}</td><td><span class='badge'>{SECTOR_MAP[name]}</span></td><td class='bullish'>{v.get('rs_rank')}%</td><td>{v.get('rsi_14')}</td><td>{v.get('ret_90d')}%</td><td>{v.get('ret_ytd')}%</td><td>{'🔥 SQUEEZE' if v.get('squeeze') else '❄️'}</td></tr>")
    html_parts.append("</table>")
    html_parts.append("<h2>🎯 個股實戰池 (Top 20 RS Rank)</h2><table><tr><th>代碼</th><th>板塊</th><th>價格</th><th>RS Rank</th><th>RSI</th><th>5D %</th><th>量比</th><th>財報日</th><th>風控止損</th></tr>")
    top_stocks = sorted(data["stocks"].items(), key=lambda x: x[1].get('rs_rank', 0), reverse=True)[:20]
    for ticker, v in top_stocks:
        vr = v.get('volume', 0) / v.get('avg_volume_20', 1) if v.get('avg_volume_20', 0) > 0 else 0
        html_parts.append(f"<tr><td><strong>{ticker}</strong></td><td>{v.get('sector')}</td><td>${v.get('close')}</td><td class='bullish'>{v.get('rs_rank')}%</td><td>{v.get('rsi_14')}</td><td>{v.get('ret_5d')}%</td><td>{vr:.2f}x</td><td>{v.get('earnings_date')}</td><td class='bearish'>${v.get('stop')}</td></tr>")
    html_parts.append("</table></body></html>")
    html_path = f"market_report_{datetime.now().strftime('%Y-%m-%d')}.html"
    with open(html_path, "w", encoding="utf-8") as f: f.write("\n".join(html_parts))
    print(f">>> HTML 報告已生成: {html_path}")

def job():
    print(f"{'='*55}\n>>> 掃描開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'='*55}")
    engine = MarketEngine()
    results = engine.run_scanner()
    if not (results["etfs"] or results["stocks"]): return
    save_to_archive(results)
    generate_ai_payload(results)
    generate_html(results)
    print(f"\n{'='*55}\n>>> 系統執行完成。ETF:{len(results['etfs'])} | 個股:{len(results['stocks'])} | VIX:{results.get('vix')}\n{'='*55}")

if __name__ == "__main__":
    job()
