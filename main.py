import os
import io
import asyncio
import logging
import re
from datetime import datetime

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv

# 引入更新後的核心邏輯
from scanner_core import scan_market, fetch_and_diagnose

load_dotenv()
TG_TOKEN = os.getenv('TG_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 背景任務：全市場掃描 (共用) ---
async def run_full_scan_background(chat_id, context, date_str, formatted_date_msg):
    """
    執行全市場掃描並傳送檔案
    """
    try:
        await context.bot.send_message(chat_id=chat_id, text=f"🚀 個股符合標準！正在執行 {formatted_date_msg} 全市場掃描 (約 60-90秒)...")
        
        results, formatted_date = await scan_market(date_str)
        
        if not results:
            await context.bot.send_message(chat_id=chat_id, text=f"🤔 奇怪，全市場掃描無結果。")
            return

        file_content = "\n".join(results)
        bio = io.BytesIO(file_content.encode('utf-8'))
        bio.name = f"TW_VCP_{formatted_date.replace('-','')}.txt"
        
        caption = (f"✅ **{formatted_date} 全市場掃描完成**\n"
                   f"共篩選出 {len(results)} 檔標的")

        await context.bot.send_document(
            chat_id=chat_id,
            document=bio,
            caption=caption,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Full scan failed: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 全市場掃描失敗: {e}")

# --- 背景任務：個股診斷 ---
async def run_diagnostic_background(chat_id, status_message_id, date_str, symbol, context):
    try:
        # 1. 執行診斷
        is_pass, report, formatted_date = await fetch_and_diagnose(symbol, date_str)
        
        # 2. 更新診斷結果訊息
        # 如果報告太長，Telegram 限制 4096 字元，稍微切一下保險
        if len(report) > 4000: report = report[:4000] + "\n...(截斷)"
        
        await context.bot.edit_message_text(
            chat_id=chat_id, 
            message_id=status_message_id, 
            text=report, 
            parse_mode='Markdown'
        )

        # 3. 如果通過，觸發全市場掃描
        if is_pass:
            # 等待 2 秒讓使用者消化一下診斷報告
            await asyncio.sleep(2)
            await run_full_scan_background(chat_id, context, date_str, formatted_date)

    except Exception as e:
        logger.error(f"Diagnostic task failed: {e}")
        await context.bot.edit_message_text(
            chat_id=chat_id, 
            message_id=status_message_id, 
            text=f"❌ 診斷過程發生錯誤: {e}"
        )

# --- 指令處理 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 台股 VCP 掃描機器人\n\n"
        "1. `/now`: 掃描今日\n"
        "2. `/251225`: 回測特定日期全市場\n"
        "3. `/251225 2330`: **診斷模式** (檢查該日某股為何不過/通過)"
    )

# 處理 /251225 (純日期 -> 全掃描)
async def history_scan_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = update.message.text.replace('/', '').strip()
    msg = await update.message.reply_text(f"⏳ 收到全掃描請求: {date_str}，運算中...")
    
    # 這裡借用 run_full_scan_background 的邏輯，但需要微調參數傳遞
    # 為了簡化，直接在這裡 create_task 調用原本的 scan_market 邏輯比較單純
    asyncio.create_task(run_scan_task_wrapper(update.effective_chat.id, msg.message_id, date_str, context))

# 處理 /251225 2330 (日期 + 股號 -> 診斷)
async def diagnostic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 解析輸入
    text = update.message.text.replace('/', '').strip() # "251225 2330"
    parts = text.split()
    
    if len(parts) < 2:
        await update.message.reply_text("❌ 格式錯誤。請輸入: `/YYMMDD 代碼`")
        return
        
    date_str = parts[0]
    symbol = parts[1]
    
    msg = await update.message.reply_text(f"👨‍⚕️ 收到診斷請求: {symbol} 於 {date_str}...\n正在調閱病歷 (資料下載中)...")
    
    asyncio.create_task(
        run_diagnostic_background(
            chat_id=update.effective_chat.id,
            status_message_id=msg.message_id,
            date_str=date_str,
            symbol=symbol,
            context=context
        )
    )

# 舊的 wrapper，給 /251225 全掃描用的
async def run_scan_task_wrapper(chat_id, msg_id, date_str, context):
    try:
        results, formatted_date = await scan_market(date_str)
        if not results:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=f"📅 {formatted_date}\n❌ 無符合標的。")
            return

        file_content = "\n".join(results)
        bio = io.BytesIO(file_content.encode('utf-8'))
        bio.name = f"TW_VCP_{formatted_date.replace('-','')}.txt"
        
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        await context.bot.send_document(
            chat_id=chat_id, document=bio, 
            caption=f"✅ **{formatted_date} 掃描報告** ({len(results)}檔)"
        )
    except Exception as e:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=f"❌ 錯誤: {e}")

async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🚀 掃描今日台股中...")
    asyncio.create_task(run_scan_task_wrapper(update.effective_chat.id, msg.message_id, None, context))

# --- 排程任務 (維持不變) ---
async def scheduled_daily_scan(app):
    while True:
        now = datetime.now()
        if now.hour == 14 and now.minute == 40:
            if TG_CHAT_ID:
                await app.bot.send_message(chat_id=TG_CHAT_ID, text="⏰ 盤後掃描啟動...")
                results, formatted_date = await scan_market(None)
                if results:
                    file_content = "\n".join(results)
                    bio = io.BytesIO(file_content.encode('utf-8'))
                    bio.name = f"Daily_{formatted_date}.txt"
                    await app.bot.send_document(chat_id=TG_CHAT_ID, document=bio, caption=f"🌞 今日 VCP ({len(results)}檔)")
                else:
                    await app.bot.send_message(chat_id=TG_CHAT_ID, text="今日無符合標的。")
            await asyncio.sleep(65)
        await asyncio.sleep(20)

if __name__ == '__main__':
    if not TG_TOKEN:
        print("❌ Error: TG_TOKEN not found")
        exit(1)

    app = ApplicationBuilder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("now", now_command))
    
    # 1. 先匹配 "日期 + 空格 + 代碼" 的格式 (診斷模式)
    # Regex 解釋: ^/ 開頭, 6個數字, 至少一個空格, 接著任意字符
    app.add_handler(MessageHandler(filters.Regex(r'^\/\d{6}\s+.+$'), diagnostic_handler))
    
    # 2. 再匹配 "純日期" 的格式 (全掃描模式)
    app.add_handler(MessageHandler(filters.Regex(r'^\/\d{6}$'), history_scan_handler))

    print("🤖 Bot started...")
    loop = asyncio.get_event_loop()
    loop.create_task(scheduled_daily_scan(app))
    app.run_polling()
