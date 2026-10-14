FROM python:3.12-slim
WORKDIR /app
COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/ .
ENV DB_PATH=/data/bot.sqlite3
CMD ["python", "bot.py"]
