FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app

# 先装依赖, 利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 默认配置 (可用 docker run -e 或 compose 覆盖)
ENV DB_PATH=/data/app.db \
    ORGANIZER_KEY=dev-organizer-key \
    HOST=0.0.0.0 \
    PORT=8000

# 初始化 SQLite 存储目录 (首次启动时自动建表)
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

CMD ["python", "-m", "app.main"]
