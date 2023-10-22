#!/bin/bash

# This script will create both the web app and the API on the local machine in containers
# using the systems hostname as the name of the web app and API. This script will check
# for dependencies before continuing the script.
# The following arguments are required:
# 1. auth_service_url: The URL of the authentication service
# 2. auth_service_secret: The secret of the authentication service

# Check and extract arguments
if [ $# -ne 2 ]; then
  echo "Usage: $0 auth_service_url auth_service_secret"
  exit 1
fi

auth_service_url=$1
auth_service_secret=$2

# Check if the script is being run as root
if [ "$EUID" -ne 0 ]
  then echo "Please run as root"
  exit
fi

# Check if docker is installed
if ! [ -x "$(command -v docker)" ]; then
  echo "Docker is not installed. Please install docker before running this script."
  exit 1
fi

# Check that there are certificates directory in /etc/ca-certificates/certs and that
# there are at least 5 files in the directory. The necessary files are:
# 1. www.crt
# 2. www.key
# 3. api.crt
# 4. api.key
# 5. root.crt
if [ ! -d "/etc/ca-certificates/certs" ]; then
  echo "The directory /etc/ca-certificates/certs does not exist. Please create the directory and add the necessary certificates."
  exit 1
fi

files=(
    "/etc/ca-certificates/certs/www.crt"
    "/etc/ca-certificates/certs/www.key"
    "/etc/ca-certificates/certs/api.crt"
    "/etc/ca-certificates/certs/api.key"
    "/etc/ca-certificates/certs/root.crt"
)

for file in "${files[@]}"; do
    if [ ! -f "$file" ]; then
        echo "The file $file does not exist. Please add the necessary certificates."
        exit 1
    fi
done

# Check if the web app container already exists, skip if it does
if [ "$(sudo docker ps -aq -f name=netgpt-webui)" ]; then
  echo "The web app container already exists. Skipping..."
else
  # Run the web app container
  sudo docker run -d -p 80:8080 -p 443:8443 --name netgpt-webui \
    -v /etc/ca-certificates/certs/www.crt:/etc/nginx/certs/certificate.crt \
    -v /etc/ca-certificates/certs/www.key:/etc/nginx/certs/certificate.key \
    kennethgrace/netgpt-webui
fi

# Check if the API container already exists, skip if it does
if [ "$(sudo docker ps -aq -f name=netgpt-api)" ]; then
  echo "The API container already exists. Skipping..."
else
  # Run the API container
  docker run -d -p 49488:49488 --name netgpt-api \
    -v /etc/ca-certificates/certs/api.crt:/app/certs/api.crt \
    -v /etc/ca-certificates/certs/api.key:/app/certs/api.key \
    -v /etc/ca-certificates/certs/root.crt:/etc/ssl/certs/root.crt \
    -e AUTH_SERVER="$auth_service_url" -e AUTH_CLIENT_SECRET="$auth_service_secret" \
     kennethgrace/netgpt-api
fi

# Pause for 10 seconds to allow the containers to start
n=10
for (( i = 0; i < n; i++ )); do
  echo "Letting containers start... $((n-i)) seconds remaining"
  # Return to the start of the line clearing the current line
  echo -en "\r\033[K"
  sleep 1
done

# Print the status of the containers to the user for confirmation
echo "The web app container is running with the following status:"
sudo docker ps -f name=netgpt-webui

echo "The API container is running with the following status:"
sudo docker ps -f name=netgpt-api