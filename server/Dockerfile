FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all server files
COPY . .

# Expose port (default 8080)
EXPOSE 8080

# Environment defaults
ENV HOST=0.0.0.0
ENV PORT=8080

# The command to run the FastAPI app via main.py
CMD ["python", "main.py"]
