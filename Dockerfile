FROM python:3.10-slim
WORKDIR /app
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "main.py:app", "--host", "0.0.0.0", "--port", "8080"]
