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
    recent_range = high.tail(15).max() - low.tail(15).min()
    if recent_range > (atr * 2.5): return False # 修改標準請在此處同步

    # 3. 量縮
    vol_sma20 = vol.tail(20).mean()
    vol_sma60 = vol.tail(60).mean()
    if vol_sma20 >= vol_sma60: return False
    
    # 4. 流動性
    if vol_sma20 < 500000: return False

    return True

# --- C. [新增] 單一股票診斷邏輯 (詳細報告用) ---
def diagnose_single_stock(df, symbol):
    """
    對單一股票進行詳細檢查，回傳報告字串與是否通過
    """
    report = []
    is_pass = True
    
    if len(df) < 65:
        return False, f"❌ 資料不足: 僅 {len(df)} 筆 (需 > 65)"

    close = df['Close']
    vol = df['Volume']
    high = df['High']
    low = df['Low']
    
    # 取最後一天的數值
    c_now = close.iloc[-1]
    
    # 1. 檢查 60MA 趨勢
    sma60 = ta.sma(close, length=60)
    ma60_now = sma60.iloc[-1]
    ma60_prev = sma60.iloc[-5]
    
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
    atr = ta.atr(high, low, close, length=14).iloc[-1]
    recent_high = high.tail(15).max()
    recent_low = low.tail(15).min()
    recent_range = recent_high - recent_low
    threshold = atr * 2.5  # 注意：這裡要跟上面的標準一致
    
    report.append(f"\n🔹 **型態收縮 (VCP Tightness)**")
    report.append(f"   ℹ️ ATR(14): {atr:.2f} | 容許震幅: {threshold:.2f}")
    report.append(f"   ℹ️ 近15日高低差: {recent_range:.2f} (高:{recent_high} 低:{recent_low})")
    
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

# --- E. [新增] 執行單一股票下載與診斷 ---
async def fetch_and_diagnose(symbol_input, date_str):
    """
    下載單一股票數據並診斷
    symbol_input: "2330" 或 "2330.TW"
    date_str: "251225"
    """
    try:
        # 1. 處理日期
        target_date = datetime.strptime(date_str, "%y%m%d")
        start_date = target_date - timedelta(days=250)
        end_date = target_date + timedelta(days=1)
        formatted_date = target_date.strftime('%Y-%m-%d')

        # 2. 處理代碼 (自動補後綴)
        symbol = symbol_input.upper()
        if not (symbol.endswith('.TW') or symbol.endswith('.TWO')):
            # 優先猜測是上市
            test_symbol = f"{symbol}.TW"
        else:
            test_symbol = symbol

        # 3. 下載數據
        # yfinance下載單一股票時，如果不存會回傳 empty dataframe
        df = yf.download(test_symbol, start=start_date, end=end_date, progress=False)
        
        # 如果 .TW 沒資料，且原始輸入沒後綴，嘗試 .TWO
        if df.empty and not (symbol.endswith('.TW') or symbol.endswith('.TWO')):
            test_symbol = f"{symbol}.TWO"
            df = yf.download(test_symbol, start=start_date, end=end_date, progress=False)

        if df.empty:
            return False, f"❌ 找不到股票數據: {symbol_input} (日期: {formatted_date})\n可能原因: 休市、代碼錯誤或已下市。", formatted_date

        # 4. 檢查日期對齊
        df.dropna(inplace=True)
        if df.empty: return False, "❌ 資料區間內無有效數據", formatted_date
        
        last_dt = df.index[-1].date()
        if last_dt != target_date.date():
            return False, f"❌ 資料日期不符\n請求: {formatted_date}\n實際最新: {last_dt}\n(可能是當天停牌或假日)", formatted_date

        # 5. 執行診斷
        is_pass, report = diagnose_single_stock(df, test_symbol)
        
        header = f"🔍 **個股診斷報告: {test_symbol}**\n📅 日期: {formatted_date}\n" + "-"*20 + "\n"
        full_report = header + report
        
        return is_pass, full_report, formatted_date

    except Exception as e:
        return False, f"❌ 診斷發生錯誤: {e}", date_str
