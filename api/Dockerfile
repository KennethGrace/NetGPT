FROM python:3.11.6-alpine3.18

# Copy all files from current directory to /app inside container
COPY . /app

# Set working directory to /app
WORKDIR /app

# Volume mount for certificate files
VOLUME /app/certs

# Expose application port 49488
EXPOSE 49488

# Update PIP and Install Requirements
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Run uvicorn server
ENTRYPOINT [ "uvicorn", "netgpt:application", "--host", "0.0.0.0", "--port", "49488", "--ssl-keyfile", "/app/certs/api.key", "--ssl-certfile", "/app/certs/api.crt" ]