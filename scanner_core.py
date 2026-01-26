import requests
import pandas as pd
import pandas_ta as ta
import yfinance as yf
import asyncio
from datetime import datetime, timedelta

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
        # 若爬蟲失敗，回傳權值股當備案
        return ['2330.TW', '2317.TW', '2454.TW', '2303.TW', '2881.TW']

# --- B. VCP 判斷邏輯 ---
def check_vcp_criteria(df):
    """
    檢查單一股票 DataFrame 是否符合 VCP 條件
    """
    if len(df) < 65: return False
    
    # 資料整理
    close = df['Close']
    vol = df['Volume']
    high = df['High']
    low = df['Low']
    
    # 1. 趨勢濾網: 價格 > 60MA 且 60MA 翻揚
    sma60 = ta.sma(close, length=60)
    if sma60.iloc[-1] is None or sma60.iloc[-5] is None: return False
    
    if close.iloc[-1] < sma60.iloc[-1]: return False
    if sma60.iloc[-1] <= sma60.iloc[-5]: return False # 斜率向上

    # 2. 價格 VCP (Tightness): 過去 15 天震幅縮小
    # 這裡放寬一點: 15天高低差 < ATR(14) * 2.5
    atr = ta.atr(high, low, close, length=14).iloc[-1]
    if pd.isna(atr) or atr == 0: return False
    
    recent_range = high.tail(15).max() - low.tail(15).min()
    if recent_range > (atr * 2.5): return False

    # 3. 成交量 VCP: 近期量縮 (20MA < 60MA)
    vol_sma20 = vol.tail(20).mean()
    vol_sma60 = vol.tail(60).mean()
    if vol_sma20 >= vol_sma60: return False
    
    # (可選) 4. 日均量濾網: 20日均量 > 500 張 (避免流動性風險)
    if vol_sma20 < 500000: # Yahoo Volume 單位是股
        return False

    return True

# --- C. 執行掃描主程式 ---
async def scan_market(target_date_str):
    """
    target_date_str: "251225" (YYMMDD) 或 None (代表今天)
    """
    try:
        # 1. 日期處理
        if target_date_str:
            target_date = datetime.strptime(target_date_str, "%y%m%d")
        else:
            target_date = datetime.now()
            # 若現在是盤中(13:30前)，可能要抓昨天，這邊假設盤後執行
        
        # yfinance 的 end date 是 exclusive，所以要 +1 天
        start_date = target_date - timedelta(days=250)
        end_date = target_date + timedelta(days=1)
        
        formatted_date = target_date.strftime('%Y-%m-%d')
        print(f"🚀 開始掃描: {formatted_date}")

        # 2. 獲取清單
        tickers = get_tw_stock_list()
        
        # 3. 分批下載 (避免 Zeabur 記憶體爆炸)
        batch_size = 200 # 建議 200-300
        valid_symbols = []

        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i+batch_size]
            try:
                # threads=True 加速下載
                data = yf.download(batch, start=start_date, end=end_date, group_by='ticker', progress=False, threads=True)
                
                if data.empty: continue

                # 遍歷這個 batch 的每一檔
                for symbol in batch:
                    try:
                        # 處理 MultiIndex (Yahoo Finance 近期改版可能回傳不同結構，需防呆)
                        if isinstance(data.columns, pd.MultiIndex):
                             df = data[symbol].copy()
                        else:
                             # 若只有一檔股票，結構不同，但在 bulk download 應該不會發生
                             continue

                        df.dropna(inplace=True)
                        if df.empty: continue
                        
                        # 確保最後一天是我們指定的日期 (處理停牌或資料缺失)
                        last_dt = df.index[-1].date()
                        if last_dt != target_date.date():
                            continue
                        
                        # 判斷 VCP
                        if check_vcp_criteria(df):
                            valid_symbols.append(symbol)
                    except Exception:
                        continue
                
                # 讓出 CPU 資源，避免卡死 event loop
                await asyncio.sleep(0.5)
                
            except Exception as e:
                print(f"⚠️ Batch download error: {e}")
                continue

        return valid_symbols, formatted_date

    except Exception as e:
        print(f"❌ Scan fatal error: {e}")
        return [], target_date_str
