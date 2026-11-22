# Image commune à orders-service et payment-service (la commande change dans le compose).
FROM python:3.11-slim
WORKDIR /app/demo-app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
