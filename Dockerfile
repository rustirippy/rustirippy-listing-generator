FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DATABASE_PATH=/data/app.db UPLOAD_DIR=/data/uploads
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
