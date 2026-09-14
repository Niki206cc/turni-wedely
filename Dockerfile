FROM mcr.microsoft.com/playwright/python:v1.51.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Rome
EXPOSE 8091
CMD ["python", "app.py"]
