FROM python:3.11-slim

WORKDIR /app
COPY server/app.py /app/server/app.py
COPY web/ /app/web/
# 宿主机若是 Windows-ACL 文件系统(fnOS 等), 拷进来的文件会带 ACL —— 光 chmod 644
# 容器里非 root 依然读不了, 所以先经 /tmp 复制一遍把 ACL 洗掉, 再统一设权限
RUN for f in server/app.py web/index.html web/icon-192.png web/icon-512.png; do \
      cp "/app/$f" "/tmp/$(basename $f)" && mv "/tmp/$(basename $f)" "/app/$f"; \
    done && chmod 644 /app/server/app.py /app/web/index.html /app/web/icon-192.png /app/web/icon-512.png

EXPOSE 8091
# 密码不给默认值: 不用环境变量指定时, 首次启动随机生成并打印在日志里
ENV PORT=8091 \
    DATA_DIR=/data \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python3", "/app/server/app.py"]
