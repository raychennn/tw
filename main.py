import os
import json
import threading
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from fastapi import FastAPI, BackgroundTasks
import telebot
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# --- 1. 初始化與環境變數 ---
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
DATA_DIR = "/app/data"
DB_PATH = os.path.join(DATA_DIR, "scan_results.json")

app = FastAPI()
tz = pytz.timezone("Asia/Taipei")
bot = telebot.TeleBot(TOKEN) if TOKEN else None

# --- 2. 資料庫邏輯 ---
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

# --- 3. 進階核心篩選引擎 ---
def scan_engine(target_date_str=None, is_auto=False):
    db = load_db()
    now_str = target_date_str if target_date_str else datetime.now(tz).strftime('%Y%m%d')
    
    if not is_auto and now_str in db:
        return db[now_str]

    target_dt = datetime.strptime(now_str, '%Y%m%d')
    end_date_str = (target_dt + timedelta(days=1)).strftime('%Y-%m-%d')
    
    # 擴大掃描清單 (範例)
    stock_list = ["2330.TW", "2454.TW", "2317.TW", "2308.TW", "2382.TW", "3034.TW", "3711.TW", "3035.TW", "8069.TWO", "3529.TWO", "6488.TWO", "3661.TW", "3017.TW"]
    
    try:
        # A. 批量抓取資料 (效能優化)
        all_data = yf.download(stock_list + ["^TWII"], end=end_date_str, period="1y", progress=False)
        
        final_picks = []
        taiex = all_data['Close']['^TWII'].ffill()

        for symbol in stock_list:
            try:
                # 提取個股數據並處理 MultiIndex
                df = pd.DataFrame({
                    'Close': all_data['Close'][symbol],
                    'High': all_data['High'][symbol],
                    'Low': all_data['Low'][symbol],
                    'Volume': all_data['Volume'][symbol]
                }).dropna()

                if len(df) < 150: continue

                c = df['Close']
                v = df['Volume']
                h = df['High']
                l = df['Low']

                # --- [條件 0] 流動性與基礎濾網 (成交值 > 5000萬) ---
                avg_turnover = (c * v).tail(20).mean()
                if avg_turnover < 50_000_000: continue
                if c.iloc[-1] < h.tail(250).max() * 0.75: continue # 股價需在一年高點 75% 以內

                # --- [條件 1] 長期趨勢 (MA 多頭排列) ---
                ma20 = c.rolling(20).mean()
                ma60 = c.rolling(60).mean()
                ma120 = c.rolling(120).mean()
                ma200 = c.rolling(200).mean() # 增加 200MA 參考

                if not (c.iloc[-1] > ma20.iloc[-1] > ma60.iloc[-1] > ma120.iloc[-1]): continue
                if not (ma120.iloc[-1] > ma120.iloc[-10]): continue # 120MA 趨勢向上

                # --- [條件 2] 相對強度 RS Score (加權回報) ---
                # 計算個股與大盤表現 (近 3, 6, 9, 12個月)
                def get_perf(ser, days): return (ser.iloc[-1] / ser.iloc[-days]) if len(ser) > days else 1
                rs_score = (get_perf(c, 63) * 0.4 + get_perf(c, 126) * 0.2 + get_perf(c, 189) * 0.2 + get_perf(c, 252) * 0.2)
                market_score = (get_perf(taiex, 63) * 0.4 + get_perf(taiex, 126) * 0.2 + get_perf(taiex, 189) * 0.2 + get_perf(taiex, 252) * 0.2)
                if rs_score < market_score: continue 

                # --- [條件 3] VCP 波動收縮與緊密度 ---
                sd5, sd20, sd60 = c.tail(5).std(), c.tail(20).std(), c.tail(60).std()
                if not (sd5 < sd20 < sd60): continue # 波動逐級收縮
                
                # 緊密度：近 5 日價格區間極小化
                tightness = (h.tail(5).max() - l.tail(5).min()) / c.iloc[-1]
                if tightness > 0.04: continue # 台股適度放寬至 4%

                # --- [條件 4] 窒息量 (Volume Dry-up) ---
                vol_ma20 = v.rolling(20).mean()
                # 條件：今日量 < 均量 50% 且為近 10 日最低量 (代表賣壓竭盡)
                is_dry_volume = (v.iloc[-1] < vol_ma20.iloc[-1] * 0.5) and (v.iloc[-1] == v.tail(10).min())
                if not is_dry_volume: continue

                final_picks.append(symbol)

            except Exception: continue

        save_db(now_str, final_picks)
        
        # Telegram 通知 (保留原功能)
        if bot and CHAT_ID:
            if final_picks:
                tv_list = [f"{('TWSE' if '.TW' in s else 'TPEX')}:{s.split('.')[0]}" for s in final_picks]
                msg = f"🚀 {now_str} VCP+RS 掃描完成：\n{', '.join(final_picks)}"
                bot.send_message(CHAT_ID, msg)
                
                txt_path = f"TV_{now_str}.txt"
                with open(txt_path, "w") as f: f.write(",".join(tv_list))
                with open(txt_path, "rb") as f: bot.send_document(CHAT_ID, f, caption=f"TradingView Import ({now_str})")
                os.remove(txt_path)
            elif not is_auto:
                bot.send_message(CHAT_ID, f"⚠️ {now_str} 盤面偏弱，無符合條件標的。")
                
        return final_picks
    except Exception as e:
        print(f"掃描出錯: {e}")
        return []

# --- 4. FastAPI 路由 ---
@app.get("/")
def home():
    return {"status": "Quantum VCP Bot Online", "db_count": len(load_db())}

@app.get("/query/{date_str}")
def manual_query(date_str: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(scan_engine, date_str)
    return {"message": f"計算請求已送出，日期：{date_str}"}

if bot:
    @bot.message_handler(commands=['start'])
    def start_cmd(message):
        bot.reply_to(message, "策略：VCP + 加權 RS + 窒息量已就緒。輸入 /yymmdd 進行回溯。")

    @bot.message_handler(regexp=r'^/\d{6}$')
    def bot_history(message):
        date_str = "20" + message.text[1:]
        scan_engine(date_str)

# 排程邏輯 (保留原設定)
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(lambda: scan_engine(is_auto=True), 'cron', day_of_week='mon-fri', hour=14, minute=0) # 建議改到收盤後 14:00
scheduler.start()

if __name__ == "__main__":
    if bot:
        threading.Thread(target=bot.infinity_polling, daemon=True).start()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
