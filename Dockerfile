# One image for the API, the frontend and ingestion; docker-compose.yml picks the command.
#   docker build -t finqa .
FROM python:3.12-slim
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 FASTEMBED_CACHE_PATH=/app/.fastembed

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the embedding model into the image, so containers never download it at runtime.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"

COPY finqa/ finqa/
COPY frontend/ frontend/
COPY eval/ eval/
COPY api.py .

EXPOSE 8080 8501
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
