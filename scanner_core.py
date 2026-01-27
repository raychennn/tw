import requests
import pandas as pd
import pandas_ta as ta
import yfinance as yf
import asyncio
import traceback
from datetime import datetime, timedelta

# --- 全域參數設定 ---
GAP_THRESHOLD = 0.04  # 跳空重置門檻 (4%)
VCP_LOOKBACK_DAYS = 10  # 檢查收斂的天數 (原5天，擴大為10天以配合跳空邏輯)
TIGHTNESS_THRESHOLD = 0.035 # 收斂標準 (放寬至 3.5% 配合 10 天區間)

# --- A. 自動獲取上市櫃清單 ---
def get_tw_stock_list():
    """從證交所與櫃買中心獲取所有股票代碼，轉為 Yahoo 格式"""
    try:
        # 上市
        url_twse = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
        res_twse = requests.get(url_twse)
        df_twse = pd.read_html(res_twse.text)[0]
        df_twse.columns = df_twse.iloc[0]
        df_twse = df_twse.iloc[1:]
        df_twse = df_twse[df_twse['有價證券別'] == '股票']
        stocks_twse = df_twse['有價證券代號及名稱'].apply(lambda x: x.split()[0] + ".TW").tolist()

        # 上櫃
        url_tpex = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"
        res_tpex = requests.get(url_tpex)
        df_tpex = pd.read_html(res_tpex.text)[0]
        df_tpex.columns = df_tpex.iloc[0]
        df_tpex = df_tpex.iloc[1:]
        df_tpex = df_tpex[df_tpex['有價證券別'] == '股票']
        stocks_tpex = df_tpex['有價證券代號及名稱'].apply(lambda x: x.split()[0] + ".TWO").tolist()

        full_list = stocks_twse + stocks_tpex
        # 排除 91 開頭 (DR股)
        full_list = [s for s in full_list if not s.startswith('91')]
        
        print(f"✅ 成功獲取 {len(full_list)} 檔台股清單")
        return full_list
    except Exception as e:
        print(f"❌ 獲取清單失敗: {e}")
        return ['2330.TW', '2317.TW', '2454.TW']

# --- Helper: Gap Reset 處理邏輯 ---
def apply_gap_reset_logic(series, gap_threshold=GAP_THRESHOLD):
    """
    輸入: 收盤價 Series (例如近 10 日)
    邏輯: 從最新日期往回推，若發現單日漲跌幅 > 4%，則截斷該日之前的數據。
    回傳: 截斷後的 Series, 是否發生重置(bool), 重置日期(str)
    """
    pct_change = series.pct_change().abs()
    
    # 從最後一天往回檢查 (跳過 pct_change 的第一個 NaN)
    # 我們希望找到"最近一次"的大跳空
    reset_idx = -1
    for i in range(len(pct_change) - 1, 0, -1):
        if pct_change.iloc[i] > gap_threshold:
            reset_idx = i
            break
            
    if reset_idx != -1:
        # 重置：保留跳空當日(含)以後的數據
        # 這樣計算震幅時，是以跳空後的新位階為基準
        cutoff_date = series.index[reset_idx]
        new_series = series.iloc[reset_idx:]
        return new_series, True, cutoff_date.strftime('%Y-%m-%d')
    
    return series, False, None

# --- B. VCP 判斷邏輯 (大量掃描用) ---
def check_vcp_criteria(df):
    """
    大量掃描專用函數: 回傳 True/False
    策略: 趨勢濾網 + Gap Reset 版的 VCP Tightness
    """
    # 0. 資料長度檢查
    if len(df) < 65: return False
    
    close = df['Close']
    vol = df['Volume']
    
    # 1. 趨勢濾網: 價格 > 60MA 且 60MA 翻揚
    sma60 = ta.sma(close, length=60)
    if sma60 is None or len(sma60.dropna()) < 5: return False
    
    # 確保最後一筆不是 NaN
    if pd.isna(sma60.iloc[-1]) or pd.isna(sma60.iloc[-5]): return False

    if close.iloc[-1] < sma60.iloc[-1]: return False  # 股價要在季線上
    if sma60.iloc[-1] <= sma60.iloc[-5]: return False # 季線斜率要向上

    # ====================================================
    # 2. VCP Tightness (含 Gap Reset)
    # ====================================================
    # 取近 N 天收盤價
    recent_closes = close.tail(VCP_LOOKBACK_DAYS)
    
    # 套用跳空重置邏輯
    effective_closes, _, _ = apply_gap_reset_logic(recent_closes)
    
    # 安全檢查：若重置後數據太少(例如剛跳空1天)，則不視為完成收斂
    if len(effective_closes) < 3: 
        return False

    max_c = effective_closes.max()
    min_c = effective_closes.min()
    current_c = close.iloc[-1]
    
    # 計算收盤價震幅百分比
    range_pct = (max_c - min_c) / current_c
    
    if range_pct > TIGHTNESS_THRESHOLD: 
        return False

    # 3. 成交量 VCP: 近期量縮 (20MA < 60MA)
    vol_sma20 = vol.tail(20).mean()
    vol_sma60 = vol.tail(60).mean()
    if vol_sma20 >= vol_sma60: return False
    
    # 4. 流動性濾網: 20日均量 > 500 張
    if vol_sma20 < 500000: return False

    return True

# --- C. 單一股票診斷邏輯 (詳細報告用) ---
def diagnose_single_stock(df, symbol):
    """
    對單一股票進行詳細檢查，回傳報告字串與是否通過
    """
    report = []
    is_pass = True
    
    # 0. 資料基礎檢查
    df = df.dropna()
    if len(df) < 65:
        return False, f"❌ 資料不足: 有效 K 線僅 {len(df)} 根 (需 > 65 根)"

    try:
        close = df['Close'].astype(float)
        vol = df['Volume'].astype(float)
    except Exception as e:
        return False, f"❌ 數據格式錯誤: {e}"
    
    c_now = close.iloc[-1]
    
    # 1. 檢查 60MA 趨勢
    sma60 = ta.sma(close, length=60)
    if sma60 is None or len(sma60.dropna()) < 5:
        return False, "❌ 無法計算 60MA"

    ma60_now = sma60.iloc[-1]
    ma60_prev = sma60.iloc[-5]
    
    report.append(f"🔹 **股價與季線 (Trend)**")
    if c_now > ma60_now:
        report.append(f"   ✅ 股價({c_now:.2f}) > 季線({ma60_now:.2f})")
    else:
        report.append(f"   ❌ 股價({c_now:.2f}) < 季線({ma60_now:.2f}) -> 趨勢偏空")
        is_pass = False

    if ma60_now > ma60_prev:
        report.append(f"   ✅ 季線翻揚")
    else:
        report.append(f"   ❌ 季線下彎")
        is_pass = False

    # ====================================================
    # 2. 檢查 VCP (含 Gap Reset 報告)
    # ====================================================
    recent_closes = close.tail(VCP_LOOKBACK_DAYS)
    
    # 套用跳空重置
    effective_closes, is_reset, reset_date = apply_gap_reset_logic(recent_closes)
    
    max_c = effective_closes.max()
    min_c = effective_closes.min()
    
    # 計算震幅
    range_val = max_c - min_c
    range_pct = range_val / c_now
    
    report.append(f"\n🔹 **收盤價收斂 (C-to-C Tightness)**")
    if is_reset:
        report.append(f"   ⚠️ **偵測到跳空!**")
        report.append(f"   ℹ️ 重置日期: {reset_date} (單日漲跌 > {GAP_THRESHOLD*100}%)")
        report.append(f"   ℹ️ 計算區間: {reset_date} ~ 今天 (共 {len(effective_closes)} 天)")
    else:
        report.append(f"   ℹ️ 區間: 近 {VCP_LOOKBACK_DAYS} 天 (無顯著跳空)")

    report.append(f"   ℹ️ 有效區間高低: {min_c:.2f} ~ {max_c:.2f}")
    report.append(f"   ℹ️ 實際震幅: {range_pct*100:.2f}% (門檻: {TIGHTNESS_THRESHOLD*100}%)")
    
    # 判定
    if len(effective_closes) < 3:
        report.append(f"   ❌ 跳空後天數過短 ({len(effective_closes)}天)，需觀察能否守穩")
        is_pass = False
    elif range_pct <= TIGHTNESS_THRESHOLD:
        report.append(f"   ✅ 符合收縮標準")
    else:
        report.append(f"   ❌ 震幅過大")
        is_pass = False

    # 3. 檢查成交量
    vol_sma20 = vol.tail(20).mean()
    vol_sma60 = vol.tail(60).mean()
    
    report.append(f"\n🔹 **成交量 (Volume)**")
    if vol_sma20 < vol_sma60:
        report.append(f"   ✅ 量縮整理 (月均 < 季均)")
    else:
        report.append(f"   ❌ 量能未縮")
        is_pass = False
        
    # 4. 流動性
    if vol_sma20 >= 500000:
        report.append(f"   ✅ 流動性足夠")
    else:
        report.append(f"   ❌ 流動性不足 (< 500張)")
        is_pass = False

    final_msg = "\n".join(report)
    return is_pass, final_msg

# --- D. 執行掃描主程式 (大量) ---
async def scan_market(target_date_str):
    try:
        # 日期處理
        if target_date_str:
            target_date = datetime.strptime(target_date_str, "%y%m%d")
        else:
            target_date = datetime.now()
        
        # 設定下載區間
        start_date = target_date - timedelta(days=250)
        end_date = target_date + timedelta(days=1)
        formatted_date = target_date.strftime('%Y-%m-%d')
        print(f"🚀 開始掃描: {formatted_date}")

        tickers = get_tw_stock_list()
        
        batch_size = 200
        valid_symbols = []

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i+batch_size]
            try:
                data = yf.download(batch, start=start_date, end=end_date, group_by='ticker', progress=False, threads=True, auto_adjust=True)
                
                if data.empty: continue

                for symbol in batch:
                    try:
                        if isinstance(data.columns, pd.MultiIndex):
                            df = data[symbol].copy()
                        else:
                            continue

                        df.columns = [c.capitalize() for c in df.columns]
                        
                        df.dropna(inplace=True)
                        if df.empty: continue
                        
                        last_dt = df.index[-1].date()
                        if last_dt != target_date.date(): continue
                        
                        if check_vcp_criteria(df):
                            valid_symbols.append(symbol)
                    except Exception:
                        continue
                
                await asyncio.sleep(0.5)
                
            except Exception as e:
                print(f"⚠️ Batch download error: {e}")
                continue

        return valid_symbols, formatted_date

    except Exception as e:
        print(f"❌ Scan fatal error: {e}")
        return [], target_date_str

# --- E. 執行單一股票下載與診斷 ---
async def fetch_and_diagnose(symbol_input, date_str):
    try:
        target_date = datetime.strptime(date_str, "%y%m%d")
        start_date = target_date - timedelta(days=250)
        end_date = target_date + timedelta(days=1)
        formatted_date = target_date.strftime('%Y-%m-%d')

        symbol = symbol_input.upper().strip()
        if not (symbol.endswith('.TW') or symbol.endswith('.TWO')):
            test_symbol = f"{symbol}.TW"
        else:
            test_symbol = symbol

        print(f"Debug: Downloading {test_symbol}...")
        df = yf.download(test_symbol, start=start_date, end=end_date, progress=False, auto_adjust=True)
        
        if df.empty and not (symbol.endswith('.TW') or symbol.endswith('.TWO')):
            test_symbol = f"{symbol}.TWO"
            print(f"Debug: Retrying with {test_symbol}...")
            df = yf.download(test_symbol, start=start_date, end=end_date, progress=False, auto_adjust=True)

        if df.empty:
            return False, f"❌ 找不到股票數據: {symbol_input}", formatted_date

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        df.columns = [c.capitalize() for c in df.columns]
        
        required_cols = ['Close', 'High', 'Low', 'Volume']
        if not all(col in df.columns for col in required_cols):
             return False, f"❌ 數據欄位缺失: {list(df.columns)}", formatted_date

        df.dropna(inplace=True)
        if df.empty: return False, "❌ 無有效數據", formatted_date
        
        last_dt = df.index[-1].date()
        if last_dt != target_date.date():
            return False, f"❌ 日期不符 (請求:{formatted_date}, 實際:{last_dt})", formatted_date

        is_pass, report = diagnose_single_stock(df, test_symbol)
        
        header = f"🔍 **個股診斷報告: {test_symbol}**\n📅 日期: {formatted_date}\n" + "-"*20 + "\n"
        full_report = header + report
        
        return is_pass, full_report, formatted_date

    except Exception as e:
        traceback.print_exc()
        return False, f"❌ 程式內部錯誤: {str(e)}", date_str

if __name__ == "__main__":
    # 測試用：直接執行掃描今天
    # import nest_asyncio
    # nest_asyncio.apply()
    # today_str = datetime.now().strftime("%y%m%d")
    # asyncio.run(scan_market(today_str))
    pass
