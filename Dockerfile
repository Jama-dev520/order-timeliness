# 订单时效看板 —— 运行镜像
#
# 只用标准库 + requests，没有编译依赖。
# 国内私有环境拉不到 Docker Hub 时，把 FROM 换成内网源，例如：
#   FROM harbor.example.com/library/python:3.13-slim
#   FROM registry.cn-hangzhou.aliyuncs.com/library/python:3.13-slim
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# 数据目录留给 PVC 覆盖；以非 root 运行，且镜像层不写字节码
RUN mkdir -p /app/data && \
    useradd -m -u 10001 appuser && \
    chown -R appuser:appuser /app

USER 10001

EXPOSE 8000

# 数据由 CronJob 抓，服务只读快照；要临时手抓就去掉 --no-fetch
CMD ["python", "serve.py", "--host", "0.0.0.0", "--port", "8000", "--no-fetch"]
