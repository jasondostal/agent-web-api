FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi uvicorn docker
COPY app.py /app/app.py
WORKDIR /app
EXPOSE 8815
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8815"]
