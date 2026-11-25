# ---- 运行镜像（含 Chromium，支持头条号草稿）----
#
# 基础镜像用 python:3.12-slim + 自己装浏览器，而不是 Playwright 官方镜像：
# 官方镜像 1.7G，且 tag 必须和 requirements.txt 里的 playwright 版本严格对齐，
# 版本一漂移就是"浏览器版本不匹配"的怪错误。自己装只多花一分多钟。
FROM python:3.12-slim

# 国内构建时换镜像站会快很多：
#   docker build --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn \
#                --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
ARG APT_MIRROR=
ARG PIP_INDEX_URL=

# 浏览器下载源。默认走 npmmirror（国内外都通，实测 19MB/s），
# 官方 cdn.playwright.dev 在国内实测 0KB/s 根本下不来。
# 想用官方源：--build-arg PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.playwright.dev
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright

# 不需要头条号就设 0，镜像小 1G 多（也可以直接用 Dockerfile.slim）
ARG WITH_BROWSER=1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 中文字体是必须的：封面要把中文标题压到图上，缺字体会变成一排方块。
# libatomic1 也别删——playwright 的 node 驱动要用，slim 镜像里没有，
# 缺了会在 `playwright install` 那一步直接报 "libatomic.so.1: cannot open"。
RUN set -eux; \
    if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl tzdata fontconfig \
        fonts-noto-cjk fonts-wqy-zenhei \
        libatomic1; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /app/requirements.txt; \
    else \
        pip install --no-cache-dir -r /app/requirements.txt; \
    fi

RUN if [ "$WITH_BROWSER" = "1" ]; then \
        export PLAYWRIGHT_DOWNLOAD_HOST="$PLAYWRIGHT_DOWNLOAD_HOST"; \
        python -m playwright install --with-deps chromium; \
    fi

COPY run.py /app/run.py
COPY app /app/app
COPY tools /app/tools

# 数据全部落在 /data，挂出来就是完整备份
ENV WXWB_DATA=/data \
    WXWB_HOST=0.0.0.0 \
    WXWB_PORT=8899

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${WXWB_PORT}/healthz" || exit 1

CMD ["python", "run.py"]
