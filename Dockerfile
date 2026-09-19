FROM python:3.11-slim

WORKDIR /app
COPY app.py /app/app.py
COPY index.html /app/index.html
COPY icon-192.png /app/icon-192.png
COPY icon-512.png /app/icon-512.png
# 宿主机权限可能很严（如 700），容器以非 root 运行会读不到 —— 统一放开
RUN chmod 644 /app/app.py /app/index.html /app/icon-192.png /app/icon-512.png

EXPOSE 8091
ENV ACCESS_PASSWORD=1027 \
    SUPER_PASSWORD=1027 \
    PORT=8091 \
    DATA_DIR=/data \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python3", "/app/app.py"]
