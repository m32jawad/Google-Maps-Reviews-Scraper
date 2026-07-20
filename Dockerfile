# One image for both the API and the workers -- the scheduler launches worker
# containers from this same image with a different command (server.worker).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY server/requirements.txt ./server/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir fastapi "uvicorn[standard]" sqlalchemy psycopg2-binary docker pydantic

COPY reviews_finder ./reviews_finder
COPY server ./server
COPY main.py find.py ./

EXPOSE 8000
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
