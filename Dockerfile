# One image for the API, the frontend, ingestion and the Spark jobs; docker-compose.yml picks the command.
#   docker build -t finqa .
FROM python:3.12-slim
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 FASTEMBED_CACHE_PATH=/app/.fastembed

# Java runs Spark (local mode, using every core of the machine).
RUN apt-get update && apt-get install -y --no-install-recommends default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-spark.txt ./
RUN pip install -r requirements.txt -r requirements-spark.txt

# Bake the embedding model into the image, so containers never download it at runtime.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"

COPY finqa/ finqa/
COPY spark/ spark/
COPY frontend/ frontend/
COPY .streamlit/ .streamlit/
COPY eval/ eval/
COPY api.py .

EXPOSE 8080 8501
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
