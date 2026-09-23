FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY . .
ENV WINDAGENT_HOME=/app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "windagent.api:app", "--host", "0.0.0.0", "--port", "8000"]

