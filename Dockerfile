FROM python:3.10-slim

# 設定工作目錄
WORKDIR /app

# 設定時區為台北 (重要！否則排程會亂掉)
RUN apt-get update && apt-get install -y tzdata \
    && ln -fs /usr/share/zoneinfo/Asia/Taipei /etc/localtime \
    && echo "Asia/Taipei" > /etc/timezone \
    && apt-get clean

# 複製依賴並安裝
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼
COPY . .

# 啟動指令
CMD ["python", "main.py"]
