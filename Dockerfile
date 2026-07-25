FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py .
COPY excel_mapping.yaml .
COPY signal_rules.yaml .
COPY templates ./templates
COPY docs ./docs

RUN mkdir -p data uploads

CMD ["python", "run.py"]
