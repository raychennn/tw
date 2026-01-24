import os
import json
import threading
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from fastapi import FastAPI, BackgroundTasks, Query
import telebot
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# --- 初始化設定 ---
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
DATA_DIR = "/app/data"
DB_PATH = os.path.join(DATA_DIR, "scan_results.json")

app = FastAPI()
bot = telebot.TeleBot(TOKEN) if TOKEN else None
tz = pytz.timezone("Asia/Taipei")

# --- 資料庫操作 ---
def load_db():
    if os.path.exists(DB_PATH):
        try:
            with open(DB_PATH, 'r') as f: return json.load(f)
        except: return {}
    return {}

def save_db(date_str, results):
    db = load_db()
    db[date_str] = results
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DB_PATH, 'w') as f:
        json.dump(db, f, indent=4)

# --- 核心篩選系統 (完整應用之前討論的條件) ---
def scan_engine(target_date_str=None, is_auto=False):
    """
    精準回溯引擎：確保 end_date 設定為目標日期的隔天，
    這樣 yf.download 抓到的最後一根 K 棒就會是目標日期當天。
    """
    db = load_db()
    now_str = target_date_str if target_date_str else datetime.now(tz).strftime('%Y%m%d')
    
    # 若非自動排程且資料庫已有資料，直接回傳
    if not is_auto and now_str in db:
        return db[now_str]

    # 設定 yfinance 終止日 (目標日 + 1天)
    target_dt = datetime.strptime(now_str, '%Y%m%d')
    end_date_str = (target_dt + timedelta(days=1)).strftime('%Y-%m-%d')
    
    # 標的池 (可持續擴充)
    stock_list = ["2330.TW", "2454.TW", "2317.TW", "2308.TW", "2382.TW", "3034.TW", "3711.TW", "3035.TW", "8069.TWO", "3529.TWO", "6488.TWO"]
    
    try:
        # 1. 抓取大盤基準 (TAIEX)
        taiex_df = yf.download("^TWII", end=end_date_str, period="1y", progress=False)
        taiex = taiex_df['Close'].iloc[:, 0] if isinstance(taiex_df.columns, pd.MultiIndex) else taiex_df['Close']
        
        final_picks = []
        for symbol in stock_list:
            df = yf.download(symbol, end=end_date_str, period="1y", progress=False)
            if df is None or df.empty or len(df) < 120: continue
            
            # 處理 yfinance 新版 MultiIndex 索引
            c = df['Close'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['Close']
            v = df['Volume'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['Volume']
            h = df['High'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['High']
            l = df['Low'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['Low']

            # --- [條件 1] 長期趨勢與穩定性 ---
            ma20, ma60, ma120 = c.rolling(20).mean(), c.rolling(60).mean(), c.rolling(120).mean()
            if not (c.iloc[-1] > ma60.iloc[-1] > ma120.iloc[-1]): continue
            if not (ma120.iloc[-1] > ma120.iloc[-2]): continue # 120SMA 斜率向上
            if not (ma60.tail(60) > ma120.tail(60)).all(): continue # 60根無死叉
            
            # --- [條件 2] 短期助漲 ---
            if not (c.tail(5) > ma20.tail(5)).all(): continue # Close > 20SMA 持續 5 根

            # --- [條件 3] RS Rank (對標加權) ---
            rs = c / taiex.reindex(c.index).ffill()
            rs_ma20 = rs.rolling(20).mean()
            if (rs > rs_ma20).tail(20).sum() < 15: continue
            if not (rs_ma20.iloc[-1] > rs_ma20.iloc[-2]): continue

            # --- [條件 4] VCP 強化與緊密度 ---
            sd5, sd20, sd60 = c.tail(5).std(), c.tail(20).std(), c.tail(60).std()
            if not (sd5 < sd20 < sd60): continue # 階梯收縮
            
            tightness = (h.tail(5).max() - l.tail(5).min()) / c.iloc[-1]
            if tightness > 0.025: continue # 緊密度 < 2.5%

            # --- [條件 5] 窒息量 ---
            vol_ma20 = v.rolling(20).mean()
            if not (v.tail(5) < vol_ma20.iloc[-1] * 0.5).any(): continue

            final_picks.append(symbol)

        save_db(now_str, final_picks)
        
        # 發送 Telegram
        if bot and final_picks:
            tv_format = [f"{('TWSE' if '.TW' in s else 'TPEX')}:{s.split('.')[0]}" for s in final_picks]
            msg = f"📅 {now_str} 篩選報告\n符合標的：{', '.join(final_picks)}"
            bot.send_message(CHAT_ID, msg)
            
            with open(f"TV_{now_str}.txt", "w") as f: f.write(",".join(tv_format))
            with open(f"TV_{now_str}.txt", "rb") as f: bot.send_document(CHAT_ID, f)
            os.remove(f"TV_{now_str}.txt")
            
        return final_picks
    except Exception as e:
        print(f"Error: {e}")
        return []

# --- 網頁路由 ---
@app.get("/")
def home():
    return {"status": "Running", "history_dates": list(load_db().keys())}

@app.get("/query/{date_str}")
def query_date(date_str: str, background_tasks: BackgroundTasks):
    """
    網頁查詢 API: /query/20260115
    """
    db = load_db()
    if date_str in db:
        return {"date": date_str, "results": db[date_str], "source": "cache"}
    
    background_tasks.add_task(scan_engine, date_str)
    return {"message": f"正在回溯計算 {date_str}，完成後將儲存並發送 Telegram。"}

# --- 定時任務與 Bot 指令 ---
@bot.message_handler(regexp=r'^/\d{6}$')
def handle_bot_history(message):
    date_str = "20" + message.text[1:]
    scan_engine(date_str)

scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(lambda: scan_engine(is_auto=True), 'cron', day_of_week='mon-fri', hour=8, minute=0)
scheduler.start()

if bot:
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
