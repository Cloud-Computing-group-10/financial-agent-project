# Container image for the QA service. Build after running ingestion locally so data/ is populated:
#   docker build -t finqa .
#   docker run -p 8080:8080 -e ANTHROPIC_API_KEY=... finqa
FROM python:3.12-slim
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 HF_HUB_OFFLINE=1 TRANSFORMERS_VERBOSITY=error
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.3.1 \
 && pip install "numpy<2" anthropic pydantic rank_bm25 beautifulsoup4 lxml requests transformers fastapi uvicorn
# Bake the embedding model into the image so the container never downloads at runtime.
RUN HF_HUB_OFFLINE=0 python -c "from transformers import AutoModel, AutoTokenizer as T; \
m='sentence-transformers/all-MiniLM-L6-v2'; T.from_pretrained(m); AutoModel.from_pretrained(m)"
COPY finqa/ finqa/
COPY api.py .
# Derived stores only (DB + vectors); raw filings are needed only for re-ingestion and the baseline.
COPY data/finqa.db data/embeddings.npy data/embeddings_ids.json data/
EXPOSE 8080
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
