FROM python:slim-bullseye

# Copy all files from current directory to /app inside container
COPY . /app

# Set working directory to /app
WORKDIR /app

# Expose application port 49488
EXPOSE 49488

# Update PIP and Install Requirements
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Run uvicorn server
ENTRYPOINT [ "uvicorn", "netgpt:application", "--host", "0.0.0.0", "--port", "49488" ]