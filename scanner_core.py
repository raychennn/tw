import requests
import pandas as pd
import pandas_ta as ta
import yfinance as yf
import asyncio
from datetime import datetime, timedelta

# --- A. 自動獲取上市櫃清單 (維持不變) ---
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
        full_list = [s for s in full_list if not s.startswith('91')]
        
        return full_list
    except Exception as e:
        print(f"❌ 獲取清單失敗: {e}")
        return ['2330.TW', '2317.TW', '2454.TW']

# --- B. VCP 判斷邏輯 (大量掃描用 - 維持極簡以求速度) ---
def check_vcp_criteria(df):
    if len(df) < 65: return False
    close = df['Close']
    vol = df['Volume']
    high = df['High']
    low = df['Low']
    
    # 1. 趨勢
    sma60 = ta.sma(close, length=60)
    if sma60.iloc[-1] is None or sma60.iloc[-5] is None: return False
    if close.iloc[-1] < sma60.iloc[-1]: return False
    if sma60.iloc[-1] <= sma60.iloc[-5]: return False

    # 2. VCP Tightness
    atr = ta.atr(high, low, close, length=14).iloc[-1]
    if pd.isna(atr) or atr == 0: return False
    recent_range = high.tail(10).max() - low.tail(10).min()
    if recent_range > (atr * 3): return False # 修改標準請在此處同步

    # 3. 量縮
    vol_sma20 = vol.tail(20).mean()
    vol_sma60 = vol.tail(60).mean()
    if vol_sma20 >= vol_sma60: return False
    
    # 4. 流動性
    if vol_sma20 < 500000: return False

    return True

# --- C. 單一股票診斷邏輯 (修正版: 加入防呆) ---
def diagnose_single_stock(df, symbol):
    """
    對單一股票進行詳細檢查，回傳報告字串與是否通過
    """
    report = []
    is_pass = True
    
    # 0. 資料基礎檢查
    # 移除任何包含 NaN 的行，確保計算指標時不會出錯
    df = df.dropna()
    
    if len(df) < 65:
        return False, f"❌ 資料不足: 有效 K 線僅 {len(df)} 根 (需 > 65 根以計算季線)"

    # 強制轉換型別，避免 yfinance 偶爾回傳 object 導致計算失敗
    try:
        close = df['Close'].astype(float)
        vol = df['Volume'].astype(float)
        high = df['High'].astype(float)
        low = df['Low'].astype(float)
    except Exception as e:
        return False, f"❌ 數據格式錯誤: 無法轉換為數字 ({e})"
    
    c_now = close.iloc[-1]
    
    # 1. 檢查 60MA 趨勢
    sma60 = ta.sma(close, length=60)
    
    # [防呆] 確保 sma60 不是 None 且資料足夠
    if sma60 is None or len(sma60.dropna()) < 5:
        return False, f"❌ 無法計算 60MA (資料長度不足或計算錯誤)"

    ma60_now = sma60.iloc[-1]
    ma60_prev = sma60.iloc[-5]
    
    # [防呆] 再次確認數值不是 NaN
    if pd.isna(ma60_now) or pd.isna(ma60_prev):
        return False, "❌ 60MA 計算結果包含無效值 (NaN)"

    report.append(f"🔹 **股價與季線 (Trend)**")
    if c_now > ma60_now:
        report.append(f"   ✅ 股價({c_now:.2f}) > 季線({ma60_now:.2f})")
    else:
        report.append(f"   ❌ 股價({c_now:.2f}) < 季線({ma60_now:.2f}) -> 趨勢偏空")
        is_pass = False

    if ma60_now > ma60_prev:
        report.append(f"   ✅ 季線翻揚 (斜率向上)")
    else:
        report.append(f"   ❌ 季線下彎 (當前 {ma60_now:.2f} < 5日前 {ma60_prev:.2f})")
        is_pass = False

    # 2. 檢查 VCP (Tightness)
    atr_series = ta.atr(high, low, close, length=14)
    
    # [防呆] 確保 ATR 有算出來
    if atr_series is None or atr_series.empty:
        return False, "❌ 無法計算 ATR (波動率指標失敗)"
        
    atr = atr_series.iloc[-1]
    if pd.isna(atr) or atr == 0:
        return False, "❌ ATR 數值無效 (NaN 或 0)"

    recent_high = high.tail(10).max()
    recent_low = low.tail(10).min()
    recent_range = recent_high - recent_low
    threshold = atr * 3 
    
    report.append(f"\n🔹 **型態收縮 (VCP Tightness)**")
    report.append(f"   ℹ️ ATR(14): {atr:.2f} | 容許震幅: {threshold:.2f}")
    report.append(f"   ℹ️ 近10日高低差: {recent_range:.2f} (高:{recent_high} 低:{recent_low})")
    
    if recent_range <= threshold:
        report.append(f"   ✅ 符合收縮條件")
    else:
        report.append(f"   ❌ 震幅過大 ({recent_range:.2f} > {threshold:.2f}) -> 籌碼不夠安定")
        is_pass = False

    # 3. 檢查成交量
    vol_sma20 = vol.tail(20).mean()
    vol_sma60 = vol.tail(60).mean()
    
    report.append(f"\n🔹 **成交量 (Volume)**")
    if vol_sma20 < vol_sma60:
        report.append(f"   ✅ 量縮整理 (月均量 {int(vol_sma20)} < 季均量 {int(vol_sma60)})")
    else:
        report.append(f"   ❌ 量能未縮 (月均量 {int(vol_sma20)} >= 季均量 {int(vol_sma60)})")
        is_pass = False
        
    # 4. 流動性
    if vol_sma20 >= 500000:
        report.append(f"   ✅ 流動性足夠")
    else:
        report.append(f"   ❌ 流動性不足 (< 500張)")
        is_pass = False

    final_msg = "\n".join(report)
    return is_pass, final_msg
# --- D. 執行掃描主程式 (維持不變) ---
async def scan_market(target_date_str):
    try:
        if target_date_str:
            target_date = datetime.strptime(target_date_str, "%y%m%d")
        else:
            target_date = datetime.now()
        
        start_date = target_date - timedelta(days=250)
        end_date = target_date + timedelta(days=1)
        formatted_date = target_date.strftime('%Y-%m-%d')
        
        tickers = get_tw_stock_list()
        batch_size = 200
        valid_symbols = []

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i+batch_size]
            try:
                data = yf.download(batch, start=start_date, end=end_date, group_by='ticker', progress=False, threads=True)
                if data.empty: continue

                for symbol in batch:
                    try:
                        if isinstance(data.columns, pd.MultiIndex):
                             df = data[symbol].copy()
                        else:
                             continue
                        df.dropna(inplace=True)
                        if df.empty: continue
                        
                        last_dt = df.index[-1].date()
                        if last_dt != target_date.date(): continue
                        
                        if check_vcp_criteria(df):
                            valid_symbols.append(symbol)
                    except Exception:
                        continue
                await asyncio.sleep(0.5)
            except Exception:
                continue

        return valid_symbols, formatted_date
    except Exception as e:
        print(f"❌ Scan error: {e}")
        return [], target_date_str
# --- E. [修正版] 執行單一股票下載與診斷 ---
async def fetch_and_diagnose(symbol_input, date_str):
    """
    下載單一股票數據並診斷 (強化資料清洗邏輯)
    """
    try:
        # 1. 處理日期
        target_date = datetime.strptime(date_str, "%y%m%d")
        start_date = target_date - timedelta(days=250)
        end_date = target_date + timedelta(days=1)
        formatted_date = target_date.strftime('%Y-%m-%d')

        # 2. 處理代碼
        symbol = symbol_input.upper()
        # 移除可能多餘的空白
        symbol = symbol.strip()
        
        # 智慧判斷後綴
        if not (symbol.endswith('.TW') or symbol.endswith('.TWO')):
            test_symbol = f"{symbol}.TW"
        else:
            test_symbol = symbol

        # 3. 下載數據 (強制 auto_adjust=True 以獲得乾淨的 Close)
        print(f"Debug: Downloading {test_symbol}...")
        df = yf.download(test_symbol, start=start_date, end=end_date, progress=False, auto_adjust=True)
        
        # 3.1 嘗試上櫃 (.TWO) 的備案
        if df.empty and not (symbol.endswith('.TW') or symbol.endswith('.TWO')):
            test_symbol = f"{symbol}.TWO"
            print(f"Debug: Retrying with {test_symbol}...")
            df = yf.download(test_symbol, start=start_date, end=end_date, progress=False, auto_adjust=True)

        if df.empty:
            return False, f"❌ 找不到股票數據: {symbol_input} (Yahoo Finance 回傳空值)\n請確認代碼是否正確 (例如 6770) 或日期是否過早。", formatted_date

        # ==========================================
        # 核心修正：資料清洗與欄位標準化
        # ==========================================
        
        # A. 處理 MultiIndex (如果 yfinance 回傳 ('Close', '6770.TW'))
        if isinstance(df.columns, pd.MultiIndex):
            # 如果是多層索引，通常第二層是 Ticker，我們只保留第一層 (Open, High...)
            df.columns = df.columns.get_level_values(0)
        
        # B. 統一欄位名稱為首字大寫 (Close, Open...) 避免大小寫錯誤
        # 有些版本回傳 'adj close', 有些是 'Close'
        df.columns = [c.capitalize() for c in df.columns]
        
        # C. 檢查必要欄位是否存在
        required_cols = ['Close', 'High', 'Low', 'Volume']
        if not all(col in df.columns for col in required_cols):
             return False, f"❌ 數據欄位缺失。\n抓到的欄位: {list(df.columns)}\n缺少必要欄位，可能是資料來源問題。", formatted_date

        # D. 移除 NaN
        df.dropna(inplace=True)
        
        if df.empty: 
            return False, "❌ 清洗後無有效數據 (全為 NaN)", formatted_date
        
        # ==========================================

        # 4. 檢查日期對齊
        last_dt = df.index[-1].date()
        # 容許誤差：如果 target_date 是週日，抓到週五也可以接受 (或是明確告知)
        # 這裡嚴格比對，若不符則告知
        if last_dt != target_date.date():
            return False, f"❌ 資料日期不符\n您的請求: {formatted_date}\n數據最新: {last_dt}\n(可能該日休市或尚未收盤)", formatted_date

        # 5. 執行診斷
        # 傳入清洗過的 df 與 正確的 symbol 名稱
        is_pass, report = diagnose_single_stock(df, test_symbol)
        
        header = f"🔍 **個股診斷報告: {test_symbol}**\n📅 日期: {formatted_date}\n" + "-"*20 + "\n"
        full_report = header + report
        
        return is_pass, full_report, formatted_date

    except Exception as e:
        import traceback
        traceback.print_exc() # 在 Console 印出詳細錯誤
        return False, f"❌ 程式內部錯誤: {str(e)}", date_str
    except Exception as e:
        return False, f"❌ 診斷發生錯誤: {e}", date_str
