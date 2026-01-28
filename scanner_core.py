import requests
import pandas as pd
import pandas_ta as ta
import yfinance as yf
import asyncio
import traceback
import math
from datetime import datetime, timedelta

# ==========================================
# ⚙️ 全域參數設定 (Strategy Configuration)
# ==========================================
# 1. 基礎 VCP 設定 (一般情況)
VCP_LOOKBACK_DAYS = 10      # 觀察天數 (配合跳空邏輯，建議維持 10 天)
DEFAULT_TIGHTNESS = 0.035   # 一般盤整的容許震幅 (3.5%)

# 2. Power Play 跳空設定 (特殊情況)
GAP_THRESHOLD = 0.04        # 判定為跳空的門檻 (4%)
# 當發生跳空時，容許震幅會自動調整為 ceil(跳空幅度)

# 3. 流動性設定
MIN_VOLUME_AVG = 500000     # 最小均量 (500張)
# ==========================================

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

# --- Helper: Gap Reset 核心邏輯 ---
def apply_gap_reset_logic(series, gap_threshold=GAP_THRESHOLD):
    """
    回傳: (截斷後的 Series, 是否跳空(bool), 跳空日期(str), 跳空幅度(float))
    """
    pct_change = series.pct_change().abs() # 取絕對值
    
    reset_idx = -1
    gap_size = 0.0
    
    # 從最後一天往回檢查 (由新到舊)
    for i in range(len(pct_change) - 1, 0, -1):
        if pct_change.iloc[i] > gap_threshold:
            reset_idx = i
            gap_size = pct_change.iloc[i]
            break
            
    if reset_idx != -1:
        # 發現跳空 -> 執行截斷 (Reset)
        cutoff_date = series.index[reset_idx]
        new_series = series.iloc[reset_idx:]
        return new_series, True, cutoff_date.strftime('%Y-%m-%d'), gap_size
    
    # 未發現跳空 -> 回傳原始數據 (保持原功能)
    return series, False, None, 0.0

# --- B. VCP 判斷邏輯 (大量掃描用) ---
def check_vcp_criteria(df):
    """
    大量掃描專用函數: 回傳 True/False
    """
    # 0. 資料長度檢查 (維持不變)
    if len(df) < 65: return False
    
    close = df['Close']
    vol = df['Volume']
    
    # 1. 趨勢濾網 (維持不變)
    sma60 = ta.sma(close, length=60)
    if sma60 is None or len(sma60.dropna()) < 5: return False
    
    if pd.isna(sma60.iloc[-1]) or pd.isna(sma60.iloc[-5]): return False
    if close.iloc[-1] < sma60.iloc[-1]: return False  # 價格在季線上
    if sma60.iloc[-1] <= sma60.iloc[-5]: return False # 季線向上

    # ====================================================
    # 2. VCP Tightness (雙軌判定)
    # ====================================================
    recent_closes = close.tail(VCP_LOOKBACK_DAYS)
    
    effective_closes, is_reset, _, gap_size = apply_gap_reset_logic(recent_closes)
    
    # 防呆: 若截斷後 K 線太少 (<3根)，視為形態未完成
    if len(effective_closes) < 3: return False

    # --- 關鍵分流邏輯 ---
    if is_reset:
        # [路徑 A] 發生 Power Play 跳空
        # 容許震幅 = 無條件進位(跳空幅度)
        # 例: 跳空 4.2% -> 容許 5.0%
        dynamic_threshold = math.ceil(gap_size * 100) / 100.0
    else:
        # [路徑 B] 無跳空 (一般 VCP)
        # 嚴格執行原本的設定 (3.5%)
        dynamic_threshold = DEFAULT_TIGHTNESS

    max_c = effective_closes.max()
    min_c = effective_closes.min()
    current_c = close.iloc[-1]
    
    range_pct = (max_c - min_c) / current_c
    
    if range_pct > dynamic_threshold: return False

    # 3. 成交量 VCP (維持不變)
    vol_sma20 = vol.tail(20).mean()
    vol_sma60 = vol.tail(60).mean()
    if vol_sma20 >= vol_sma60: return False
    
    # 4. 流動性濾網 (維持不變)
    if vol_sma20 < MIN_VOLUME_AVG: return False

    return True

# --- C. 單一股票診斷邏輯 (詳細報告用) ---
def diagnose_single_stock(df, symbol):
    """
    產生詳細診斷報告
    """
    report = []
    is_pass = True
    
    df = df.dropna()
    if len(df) < 65:
        return False, f"❌ 資料不足: 有效 K 線僅 {len(df)} 根"

    try:
        close = df['Close'].astype(float)
        vol = df['Volume'].astype(float)
    except Exception as e:
        return False, f"❌ 數據格式錯誤: {e}"
    
    c_now = close.iloc[-1]
    
    # 1. 趨勢檢查
    sma60 = ta.sma(close, length=60)
    if sma60 is None: return False, "❌ 無法計算 MA"
    ma60_now = sma60.iloc[-1]
    ma60_prev = sma60.iloc[-5]
    
    report.append(f"🔹 **Trend (趨勢)**")
    if c_now > ma60_now:
        report.append(f"   ✅ 股價 > 季線")
    else:
        report.append(f"   ❌ 股價跌破季線")
        is_pass = False

    if ma60_now > ma60_prev:
        report.append(f"   ✅ 季線翻揚")
    else:
        report.append(f"   ❌ 季線下彎")
        is_pass = False

    # 2. VCP Tightness 檢查
    recent_closes = close.tail(VCP_LOOKBACK_DAYS)
    effective_closes, is_reset, reset_date, gap_size = apply_gap_reset_logic(recent_closes)
    
    max_c = effective_closes.max()
    min_c = effective_closes.min()
    range_val = max_c - min_c
    range_pct = range_val / c_now
    
    # 設定顯示變數
    if is_reset:
        dynamic_threshold = math.ceil(gap_size * 100) / 100.0
        thresh_str = f"{dynamic_threshold*100:.0f}% (Power Play 動態調整)"
    else:
        dynamic_threshold = DEFAULT_TIGHTNESS
        thresh_str = f"{dynamic_threshold*100:.1f}% (標準 VCP 設定)"

    report.append(f"\n🔹 **Tightness (收斂)**")
    if is_reset:
        report.append(f"   ⚡ **偵測到跳空 (Power Play)**")
        report.append(f"   ℹ️ 跳空日期: {reset_date} | 幅度: {gap_size*100:.2f}%")
        report.append(f"   ℹ️ 重置後計算區間: {len(effective_closes)} 天")
    else:
        report.append(f"   ℹ️ 一般盤整模式 (近 {VCP_LOOKBACK_DAYS} 天無顯著跳空)")

    report.append(f"   ℹ️ 實際震幅: {range_pct*100:.2f}%")
    report.append(f"   ℹ️ 容許門檻: {thresh_str}")
    
    if len(effective_closes) < 3:
        report.append(f"   ❌ 跳空後天數過短 (<3天)，形態未確認")
        is_pass = False
    elif range_pct <= dynamic_threshold:
        report.append(f"   ✅ 符合標準")
    else:
        report.append(f"   ❌ 震幅過大 (超標)")
        is_pass = False

    # 3. 成交量檢查
    vol_sma20 = vol.tail(20).mean()
    vol_sma60 = vol.tail(60).mean()
    
    report.append(f"\n🔹 **Volume (成交量)**")
    if vol_sma20 < vol_sma60:
        report.append(f"   ✅ 量縮整理 (月均 < 季均)")
    else:
        report.append(f"   ❌ 量能未縮")
        is_pass = False
        
    # 4. 流動性檢查
    if vol_sma20 >= MIN_VOLUME_AVG:
        report.append(f"   ✅ 流動性足夠")
    else:
        report.append(f"   ❌ 流動性不足")
        is_pass = False

    final_msg = "\n".join(report)
    return is_pass, final_msg

# --- D. 執行掃描主程式 (大量) ---
async def scan_market(target_date_str):
    try:
        if target_date_str:
            target_date = datetime.strptime(target_date_str, "%y%m%d")
        else:
            target_date = datetime.now()
        
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
                # 下載數據
                data = yf.download(batch, start=start_date, end=end_date, group_by='ticker', progress=False, threads=True, auto_adjust=True)
                
                if data.empty: continue

                for symbol in batch:
                    try:
                        # 資料清洗
                        if isinstance(data.columns, pd.MultiIndex):
                            df = data[symbol].copy()
                        else:
                            continue

                        df.columns = [c.capitalize() for c in df.columns]
                        df.dropna(inplace=True)
                        if df.empty: continue
                        
                        # 日期檢核
                        last_dt = df.index[-1].date()
                        if last_dt != target_date.date(): continue
                        
                        # 核心篩選
                        if check_vcp_criteria(df):
                            valid_symbols.append(symbol)
                    except Exception:
                        continue
                
                # 避免請求過快
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
    pass
