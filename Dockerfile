FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt pyproject.toml README.md ./
COPY src ./src
COPY main.py ./
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .

RUN mkdir -p /app/data

CMD ["python", "main.py"]
