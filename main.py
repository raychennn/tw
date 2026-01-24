import os
import json
import threading
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from fastapi import FastAPI, BackgroundTasks
import telebot
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# --- 1. 初始化與環境變數防護 ---
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
DATA_DIR = "/app/data"
DB_PATH = os.path.join(DATA_DIR, "scan_results.json")

app = FastAPI()
tz = pytz.timezone("Asia/Taipei")
bot = telebot.TeleBot(TOKEN) if TOKEN else None

# --- 2. 資料庫邏輯 (Zeabur Volume 持久化) ---
def load_db():
    if os.path.exists(DB_PATH):
        try:
            with open(DB_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: return {}
    return {}

def save_db(date_str, results):
    db = load_db()
    db[date_str] = results
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=4, ensure_ascii=False)

# --- 3. 核心篩選引擎 (完整滿足 VCP + RS Rank + 20SMA + 窒息量) ---
def scan_engine(target_date_str=None, is_auto=False):
    db = load_db()
    now_str = target_date_str if target_date_str else datetime.now(tz).strftime('%Y%m%d')
    
    # 歷史回顧檢查：非自動執行且有快取則跳過計算
    if not is_auto and now_str in db:
        return db[now_str]

    # 設定 yfinance 終止日 (目標日+1) 以獲取當天收盤價
    target_dt = datetime.strptime(now_str, '%Y%m%d')
    end_date_str = (target_dt + timedelta(days=1)).strftime('%Y-%m-%d')
    
    # 台股掃描清單 (可根據需要自行增減)
    stock_list = ["2330.TW", "2454.TW", "2317.TW", "2308.TW", "2382.TW", "3034.TW", "3711.TW", "3035.TW", "8069.TWO", "3529.TWO", "6488.TWO"]
    
    try:
        # A. 抓取加權指數 (TAIEX)
        taiex_data = yf.download("^TWII", end=end_date_str, period="1y", progress=False)
        taiex = taiex_data['Close'].iloc[:, 0] if isinstance(taiex_data.columns, pd.MultiIndex) else taiex_data['Close']
        
        final_picks = []
        for symbol in stock_list:
            df = yf.download(symbol, end=end_date_str, period="1y", progress=False)
            if df is None or df.empty or len(df) < 120: continue
            
            # yfinance 多重索引處理
            c = df['Close'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['Close']
            v = df['Volume'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['Volume']
            h = df['High'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['High']
            l = df['Low'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['Low']

            # --- [條件 1] 長期趨勢 (60>120 且 60根無死叉) ---
            ma20, ma60, ma120 = c.rolling(20).mean(), c.rolling(60).mean(), c.rolling(120).mean()
            if not (c.iloc[-1] > ma60.iloc[-1] > ma120.iloc[-1]): continue
            if not (ma120.iloc[-1] > ma120.iloc[-2]): continue
            if not (ma60.tail(60) > ma120.tail(60)).all(): continue
            
            # --- [條件 2] 短期助漲 (Close > 20SMA 持續 5 根) ---
            if not (c.tail(5) > ma20.tail(5)).all(): continue

            # --- [條件 3] RS Rank (對標加權指數) ---
            rs = c / taiex.reindex(c.index).ffill()
            rs_ma20 = rs.rolling(20).mean()
            if (rs > rs_ma20).tail(20).sum() < 15: continue
            if not (rs_ma20.iloc[-1] > rs_ma20.iloc[-2]): continue

            # --- [條件 4] VCP 階梯收縮與緊密度 (<2.5%) ---
            sd5, sd20, sd60 = c.tail(5).std(), c.tail(20).std(), c.tail(60).std()
            if not (sd5 < sd20 < sd60): continue
            
            tightness = (h.tail(5).max() - l.tail(5).min()) / c.iloc[-1]
            if tightness > 0.025: continue

            # --- [條件 5] 窒息量 (<20MA Vol * 50%) ---
            vol_ma20 = v.rolling(20).mean()
            if not (v.tail(5) < vol_ma20.iloc[-1] * 0.5).any(): continue

            final_picks.append(symbol)

        save_db(now_str, final_picks)
        
        # Telegram 發送訊息與檔案
        if bot and CHAT_ID:
            if final_picks:
                tv_list = [f"{('TWSE' if '.TW' in s else 'TPEX')}:{s.split('.')[0]}" for s in final_picks]
                msg = f"📊 {now_str} 篩選結果：\n{', '.join(final_picks)}"
                bot.send_message(CHAT_ID, msg)
                
                txt_path = f"TV_{now_str}.txt"
                with open(txt_path, "w") as f: f.write(",".join(tv_list))
                with open(txt_path, "rb") as f: bot.send_document(CHAT_ID, f, caption=f"TradingView Import ({now_str})")
                os.remove(txt_path)
            elif not is_auto:
                bot.send_message(CHAT_ID, f"⚠️ {now_str} 無符合條件標的。")
                
        return final_picks
    except Exception as e:
        print(f"掃描出錯: {e}")
        return []

# --- 4. FastAPI 路由與排程 ---
@app.get("/")
def home():
    return {"status": "Bot Online", "db_count": len(load_db())}

@app.get("/query/{date_str}")
def manual_query(date_str: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(scan_engine, date_str)
    return {"message": f"計算請求已送出，日期：{date_str}"}

if bot:
    @bot.message_handler(commands=['start'])
    def start_cmd(message):
        bot.reply_to(message, "機器人已上線。輸入 /yymmdd 進行回溯。")

    @bot.message_handler(regexp=r'^/\d{6}$')
    def bot_history(message):
        date_str = "20" + message.text[1:]
        scan_engine(date_str)

scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(lambda: scan_engine(is_auto=True), 'cron', day_of_week='mon-fri', hour=8, minute=0)
scheduler.start()

if __name__ == "__main__":
    if bot:
        threading.Thread(target=bot.infinity_polling, daemon=True).start()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
