FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy ./proxy
COPY upstream ./upstream

# 实际启动命令由 docker-compose 的 command 指定（同一镜像，两种角色）
EXPOSE 8000 8001
