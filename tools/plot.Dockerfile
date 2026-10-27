# 試験結果の図を描くコンテナ（matplotlib と日本語フォント）。手元に何も入れずに使う。
#   docker build -t home-router/plot -f tools/plot.Dockerfile tools
#   docker run --rm -v "$PWD":/w -w /w home-router/plot python scripts/plot_results.py
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir --root-user-action=ignore matplotlib==3.10.0
