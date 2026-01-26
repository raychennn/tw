import os
import io
import asyncio
import logging
from datetime import datetime
import re

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

# 引入核心邏輯
from scanner_core import scan_market

# 載入環境變數
load_dotenv()
TG_TOKEN = os.getenv('TG_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')

# 設定 Log
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 背景任務執行器 (防 Timeout 核心) ---
async def run_scan_task_background(chat_id, status_message_id, date_str, context: ContextTypes.DEFAULT_TYPE):
    """
    這是真正執行掃描的背景函數
    """
    try:
        # 1. 執行掃描
        results, formatted_date = await scan_market(date_str)
        
        # 2. 準備結果
        if not results:
            final_text = f"📅 **{formatted_date} 掃描報告**\n❌ 沒有發現符合 VCP 形態的標的。"
            await context.bot.edit_message_text(
                chat_id=chat_id, 
                message_id=status_message_id, 
                text=final_text, 
                parse_mode='Markdown'
            )
            return

        # 3. 製作檔案 (避免訊息過長)
        file_content = "\n".join(results)
        file_name = f"TW_VCP_{formatted_date.replace('-','')}.txt"
        
        # 使用 BytesIO 在記憶體中產生檔案
        bio = io.BytesIO(file_content.encode('utf-8'))
        bio.name = file_name
        
        caption = (f"✅ **{formatted_date} 掃描完成**\n"
                   f"共篩選出 {len(results)} 檔標的\n"
                   f"條件: 60MA翻揚 + 量縮 + 窄幅整理")

        # 4. 刪除原本的「處理中」訊息，改發檔案
        await context.bot.delete_message(chat_id=chat_id, message_id=status_message_id)
        await context.bot.send_document(
            chat_id=chat_id,
            document=bio,
            caption=caption,
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Background task failed: {e}")
        await context.bot.edit_message_text(
            chat_id=chat_id, 
            message_id=status_message_id, 
            text=f"❌ 掃描過程發生錯誤: {e}"
        )

# --- 指令處理 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 台股 VCP 掃描機器人已就緒！\n\n"
        "1. 輸入 `/now` : 立即掃描今日\n"
        "2. 輸入 `/240101` (YYMMDD) : 回測特定日期"
    )

async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. 快速回應 (Ack)
    msg = await update.message.reply_text("🚀 收到指令！正在掃描今日台股，請稍候 (約 1-3 分鐘)...")
    
    # 2. 丟入背景執行 (不卡住 Telegram)
    asyncio.create_task(
        run_scan_task_background(
            chat_id=update.effective_chat.id,
            status_message_id=msg.message_id,
            date_str=None, # None 代表今天
            context=context
        )
    )

async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.replace('/', '').strip()
    
    # 正規表達式確保是 6 位數字
    if not re.fullmatch(r'\d{6}', user_input):
        return # 忽略非日期格式
        
    # 1. 快速回應
    msg = await update.message.reply_text(f"⏳ 收到回測請求: 20{user_input[:2]}/{user_input[2:4]}/{user_input[4:]}，運算中...")
    
    # 2. 丟入背景
    asyncio.create_task(
        run_scan_task_background(
            chat_id=update.effective_chat.id,
            status_message_id=msg.message_id,
            date_str=user_input,
            context=context
        )
    )

# --- 定時排程 (每天 14:40 盤後) ---
async def scheduled_daily_scan(app):
    while True:
        now = datetime.now()
        # 設定時區 (Zeabur 預設 UTC，這裡簡單用 +8 換算，或在 env 設定 TZ)
        # 假設系統時間已經是 Asia/Taipei (我們會在 Docker/Env 設定)
        
        # 每天 14:40 執行
        if now.hour == 14 and now.minute == 40:
            if TG_CHAT_ID:
                await app.bot.send_message(chat_id=TG_CHAT_ID, text="⏰ 定時任務啟動: 盤後掃描...")
                # 呼叫背景任務邏輯 (稍微改寫一下以適應無 update 物件的情況)
                results, formatted_date = await scan_market(None)
                if results:
                    file_content = "\n".join(results)
                    bio = io.BytesIO(file_content.encode('utf-8'))
                    bio.name = f"Daily_Scan_{formatted_date}.txt"
                    await app.bot.send_document(
                        chat_id=TG_CHAT_ID, 
                        document=bio, 
                        caption=f"🌞 **今日盤後 VCP 掃描**\n數量: {len(results)}"
                    )
                else:
                    await app.bot.send_message(chat_id=TG_CHAT_ID, text="今日無符合標的。")
            
            # 避免同一分鐘重複執行，睡 65 秒
            await asyncio.sleep(65)
        
        await asyncio.sleep(20)

# --- 主程式 ---
if __name__ == '__main__':
    # 確保 Token 存在
    if not TG_TOKEN:
        print("❌ Error: TG_TOKEN not found in .env")
        exit(1)

    app = ApplicationBuilder().token(TG_TOKEN).build()

    # 註冊 Handler
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("now", now_command))
    # 捕捉 "/251225" 格式 (Regex)
    app.add_handler(MessageHandler(filters.Regex(r'^\/\d{6}$'), history_handler))

    print("🤖 Bot started...")
    
    # 啟動排程 loop
    loop = asyncio.get_event_loop()
    loop.create_task(scheduled_daily_scan(app))
    
    app.run_polling()
