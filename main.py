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

# --- 1. 初始化環境變數與安全檢查 ---
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
DATA_DIR = "/app/data"
DB_PATH = os.path.join(DATA_DIR, "scan_results.json")

app = FastAPI()
tz = pytz.timezone("Asia/Taipei")

# 建立 Bot 實例 (若 Token 缺失則不啟動 Bot 功能)
bot = telebot.TeleBot(TOKEN) if TOKEN else None
if not bot:
    print("⚠️ 警告: TELEGRAM_BOT_TOKEN 未設置，Bot 功能將無法運行。")

# --- 2. 資料庫操作 (Zeabur Volume) ---
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
        json.dump(db, f, indent=4, ensure_ascii=False)

# --- 3. 核心篩選引擎 (VCP + RS Rank + 窒息量) ---
def scan_engine(target_date_str=None, is_auto=False):
    db = load_db()
    now_str = target_date_str if target_date_str else datetime.now(tz).strftime('%Y%m%d')
    
    # 歷史回顧檢查：若已有紀錄且非自動執行，則不重複計算
    if not is_auto and now_str in db:
        return db[now_str]

    # 設定 yfinance 終止日 (目標日 + 1天) 確保包含目標日數據
    target_dt = datetime.strptime(now_str, '%Y%m%d')
    end_date_str = (target_dt + timedelta(days=1)).strftime('%Y-%m-%d')
    
    # 預設掃描標的清單 (可隨時在 GitHub 修改此列表)
    stock_list = ["2330.TW", "2454.TW", "2317.TW", "2308.TW", "2382.TW", "3034.TW", "3711.TW", "3035.TW", "8069.TWO", "3529.TWO", "6488.TWO"]
    
    try:
        # A. 抓取加權指數基準 (TAIEX)
        taiex_df = yf.download("^TWII", end=end_date_str, period="1y", progress=False)
        taiex = taiex_df['Close'].iloc[:, 0] if isinstance(taiex_df.columns, pd.MultiIndex) else taiex_df['Close']
        
        final_picks = []
        for symbol in stock_list:
            df = yf.download(symbol, end=end_date_str, period="1y", progress=False)
            if df is None or df.empty or len(df) < 120: continue
            
            # 處理 yfinance 新版多重索引索引
            c = df['Close'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['Close']
            v = df['Volume'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['Volume']
            h = df['High'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['High']
            l = df['Low'].iloc[:, 0] if isinstance(df.columns, pd.MultiIndex) else df['Low']

            # --- [條件 1] 長期趨勢與穩定性 (60/120 SMA) ---
            ma20, ma60, ma120 = c.rolling(20).mean(), c.rolling(60).mean(), c.rolling(120).mean()
            if not (c.iloc[-1] > ma60.iloc[-1] > ma120.iloc[-1]): continue
            if not (ma120.iloc[-1] > ma120.iloc[-2]): continue # 120SMA 斜率向上
            if not (ma60.tail(60) > ma120.tail(60)).all(): continue # 60根無死叉
            
            # --- [條件 2] 短期助漲 (20SMA) ---
            if not (c.tail(5) > ma20.tail(5)).all(): continue # Close > 20SMA 持續 5 根

            # --- [條件 3] RS Rank (對標加權指數) ---
            rs = c / taiex.reindex(c.index).ffill() # 確保 index 對齊
            rs_ma20 = rs.rolling(20).mean()
            if (rs > rs_ma20).tail(20).sum() < 15: continue # 20根內達15根強勢
            if not (rs_ma20.iloc[-1] > rs_ma20.iloc[-2]): continue # RS均線斜率向上

            # --- [條件 4] VCP 階梯收縮與緊密度 ---
            sd5, sd20, sd60 = c.tail(5).std(), c.tail(20).std(), c.tail(60).std()
            if not (sd5 < sd20 < sd60): continue # SD 階梯式收縮
            
            tightness = (h.tail(5).max() - l.tail(5).min()) / c.iloc[-1]
            if tightness > 0.025: continue # 緊密度 < 2.5%

            # --- [條件 5] 窒息量 ---
            vol_ma20 = v.rolling(20).mean()
            if not (v.tail(5) < vol_ma20.iloc[-1] * 0.5).any(): continue # 出現成交量 < 20MA 50%

            final_picks.append(symbol)

        save_db(now_str, final_picks)
        
        # 4. Telegram 發送邏輯
        if bot and CHAT_ID:
            if final_picks:
                tv_list = [f"{('TWSE' if '.TW' in s else 'TPEX')}:{s.split('.')[0]}" for s in final_picks]
                msg = f"✅ {now_str} 篩選完成\n符合標的：{', '.join(final_picks)}"
                bot.send_message(CHAT_ID, msg)
                
                # 生成 TradingView TXT
                txt_path = f"TV_{now_str}.txt"
                with open(txt_path, "w") as f: f.write(",".join(tv_list))
                with open(txt_path, "rb") as f: bot.send_document(CHAT_ID, f, caption=f"TradingView 匯入檔 ({now_str})")
                os.remove(txt_path)
            else:
                if not is_auto: # 自動執行時若沒選到標的，可選擇不發訊息
                    bot.send_message(CHAT_ID, f"⚠️ {now_str} 無符合篩選條件之標的。")
            
        return final_picks
    except Exception as e:
        print(f"掃描引擎報錯: {e}")
        return []

# --- 5. 網頁路由 API ---
@app.get("/")
def health_check():
    return {"status": "Service Active", "history_entries": list(load_db().keys())}

@app.get("/query/{date_str}")
def web_query(date_str: str, background_tasks: BackgroundTasks):
    """手動透過網頁觸發回溯: /query/20260115"""
    background_tasks.add_task(scan_engine, date_str)
    return {"message": f"正在計算 {date_str}，完成後將透過 Telegram 通知並存入資料庫。"}

# --- 6. Telegram Bot 指令解析 ---
if bot:
    @bot.message_handler(commands=['start'])
    def welcome_msg(message):
        bot.reply_to(message, "台股 VCP 策略機器人已連線。\n\n指令範例：\n/260115 - 查詢指定日期標的")

    @bot.message_handler(regexp=r'^/\d{6}$')
    def bot_history_query(message):
        date_str = "20" + message.text[1:]
        bot.send_message(message.chat.id, f"正在啟動 {date_str} 歷史回溯引擎...")
        scan_engine(date_str)

# --- 7. 排程與背景執行緒 ---
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
# 定時任務：週一至週五 08:00
scheduler.add_job(lambda: scan_engine(is_auto=True), 'cron', day_of_week='mon-fri', hour=8, minute=0)
scheduler.start()

if __name__ == "__main__":
    if bot:
        # 將 Bot 監聽放進獨立執行緒，避免阻塞 FastAPI 啟動
        threading.Thread(target=bot.infinity_polling, daemon=True).start()
    
    import uvicorn
    # Zeabur 建議監聽 0.0.0.0 並使用 EXPOSE 的 Port
    uvicorn.run(app, host="0.0.0.0", port=8080)
