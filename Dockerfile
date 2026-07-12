FROM python:3.11-slim

# Keeps Python from writing .pyc files and enables unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Set the working directory in the container
WORKDIR /app

# Copy requirements first so Docker caches this layer -
# pip install only re-runs when requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Config is provided at runtime with `--env-file` (see deploy.sh) rather than
# baked into the image, so the same image works locally and on EC2 and never
# carries secrets. AWS credentials come from the EC2 instance role; the Gemini
# key is passed through the env file.
EXPOSE 5001
CMD ["python", "app.py"]