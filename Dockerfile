# ⚠️ 为什么是 3.13 而不是 3.11：
#   本机拉 Docker Hub 会失败 —— `auth.docker.io` 解析到 IPv6（2a03:2880:...）后连不上，
#     failed to fetch oauth token: dial tcp [2a03:2880:...]:443: connectex...
#   而本机**已经有 python:3.13-slim**（Immich 那套留下的），用它就完全不用拉基础镜像。
#   上游库要求 `requires_python >= 3.10`，3.13 满足。
#   （更好的做法是等能连上 registry 后换成带 digest 的固定标签，见 README"已知限制"。）
FROM python:3.13-slim

# ⚠️⚠️ 代理只能用 ARG，**绝不能用 ENV**。
#
#   如果代理进了运行时的环境变量，容器里 qqmusic_api 的 HTTP 客户端就会走代理，
#   而 QQ 音乐只认**中国大陆 IP** —— 代理出口在国外的话，所有接口直接失败。
#   ARG 只在 build 阶段可见，不会留在镜像的运行环境里。
#
#   （实测：本机代理出口是 AWS 首尔 43.201.70.165，拿它访问 QQ 必挂。）
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    QQ_DATA_DIR=/data

WORKDIR /app

# 先装依赖再拷代码 —— 改 app.py 时不会重新装一遍依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

VOLUME ["/data"]
EXPOSE 3210

# 单 worker：上游客户端自己维护连接与会话，多 worker 反而会重复登录态、更容易触发风控
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3210", \
     "--log-level", "info", "--no-access-log"]
