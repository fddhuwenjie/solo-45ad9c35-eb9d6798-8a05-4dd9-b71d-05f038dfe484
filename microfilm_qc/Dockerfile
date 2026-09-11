# 微缩胶片质检工作台容器镜像（本地分析，无需外网服务）
# 固定使用受支持的 Python 小版本：Pillow 为含 C 扩展（_imaging）的二进制包，
# 必须在构建时按本解释器版本安装匹配 wheel，不能从宿主机复制 site-packages。
FROM python:3.11-slim

WORKDIR /app

# 先装依赖，利用构建层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用本体（.dockerignore 已排除数据与随附 site-packages）
COPY qc_core ./qc_core
COPY templates ./templates
COPY static ./static
COPY app.py ./

# SQLite 数据库、帧图像与导出文件写在 /app/data
RUN mkdir -p /app/data
VOLUME ["/app/data"]

ENV MICROFILM_QC_HOST=0.0.0.0 \
    MICROFILM_QC_PORT=5000
EXPOSE 5000

# 简单内置启动；生产可换 gunicorn "app:app"
CMD ["python", "app.py"]
