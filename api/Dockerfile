FROM python:3.10-alpine3.18

# Copy all files from current directory to /app inside container
COPY . /app

# Set working directory to /app
WORKDIR /app

# Volume mount for certificate files
VOLUME /app/certs

# Expose application port 49488
EXPOSE 49488

# Install requirements without any cache
RUN python3 -m pip install --no-cache-dir -r requirements.txt

# Run uvicorn server
ENTRYPOINT [ "uvicorn", "netgpt:application", "--host", "0.0.0.0", "--port", "49488", "--ssl-keyfile", "/app/certs/api.key", "--ssl-certfile", "/app/certs/api.crt" ]