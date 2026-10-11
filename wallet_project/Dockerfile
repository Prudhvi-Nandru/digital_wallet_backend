FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
COPY docs ./docs
COPY migrations ./migrations
COPY README.md .
COPY .env.example .env.example
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
