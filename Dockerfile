FROM python:3.11-slim

WORKDIR /app

COPY app.py /app/app.py
COPY static /app/static

ENV HOST=0.0.0.0
ENV PORT=8088
ENV UPLOAD_DATA_DIR=/app/data
ENV MAX_UPLOAD_MB=30

EXPOSE 8088

CMD ["python", "app.py"]
