# NetGPT: The Network Engineering GPT

> Hello to all interested parties. Unfortunately due to time constraints, I have had to shelve this project for the time being. It needs some significant refactoring to expand funtionality and improve integration of external services. **Please consider this a WIP for now**.

[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](https://mit-license.org)
[![Docker Hub](https://img.shields.io/badge/Docker_Hub-NetGPT-blue?style=flat-square)](https://hub.docker.com/r/kennethgrace/netgpt-webui)
[![Author](https://img.shields.io/badge/Author-Kenneth_J_Grace-orange?style=flat-square)](https://kennethjeffersongrace.com)

## Introduction

NetGPT is a Web App and API for performing network engineering tasks using GPT natural language processing. It is built using [FastAPI](https://fastapi.tiangolo.com/) and [React](https://reactjs.org/). The Application and the API are containerized using [Docker](https://www.docker.com/). Authentication to the application is provided through [KeyCloak](https://www.keycloak.org/), an open source identity and access management solution.

### Table of Contents

- [Introduction](#introduction)
  - [Table of Contents](#table-of-contents)
- [Deployment](#deployment)
  - [Docker Containers](#docker-containers)
  - [Docker Compose](#docker-compose)
  - [Auto-Deploy.sh](#auto-deploysh)
- [Authentication](#authentication)
  - [Using a KeyCloak Server](#using-a-keycloak-server)
- [Certificates](#certificates)
  - [Docker Compose](#docker-compose-1)
  - [Auto-Deploy.sh](#auto-deploysh-1)
  - [Generating Self-Signed Certificates](#generating-self-signed-certificates)
- [Configuration](#configuration)
  - [API Configuration](#api-configuration)
    - [Configuration File](#configuration-file)
    - [Environment Variables](#environment-variables)

## Deployment

Deployment of NetGPT is dependent on several components. The easiest way to deploy NetGPT is using Docker. You can also use Docker Compose or the `auto-deploy.sh` script to deploy NetGPT.

### Docker Containers

The easiest way to run NetGPT is using Docker. The Docker images are available on [Docker Hub](https://hub.docker.com/u/kennethgrace). The application is available as `kennethgrace/netgpt-webui` and the API is available as `kennethgrace/netgpt-api`. The images are built automatically from the `main` branch of this repository. To install Docker, follow the instructions on the [Docker website](https://docs.docker.com/get-docker/). Once Docker is installed, you can run the containers according the [Certificates](#certificates) and [Configuration](#configuration) sections below.

### Docker Compose

If you want to run both the application and the API, you can use the included `docker-compose.yml` file. To run the
application and the API, use the following command. 

```bash
git clone https://github.com/kennethgrace/netgpt.git
cd netgpt
docker compose up -d
```

You will need to provide certificates as described in the [Certificates](#certificates) section below. You may also need to modify the `docker-compose.yml` file to match your environment according to the [Configuration](#configuration) section below.

### Auto-Deploy.sh

If you want to run the application and the API on a single node, you can use the `auto-deploy.sh` script. The script will check for necessary dependencies and run the application and API in Docker containers.

```bash
./auto-deploy.sh [auth_server_url] [auth_client_secret]
```

## Certificates

Even in development, NetGPT requires mutually trusted certificates for the application, the API, and the authentication server. You can provide your own certificates or generate self-signed certificates using the provided `generate.sh` script. The below commands assume the certificates are available in the `/etc/ca-certificates/certs` directory.

_For the Application:_

```bash
docker run -d -p 80:8080 -p 443:8443 --name netgpt-webui \
 -v /etc/ca-certificates/certs/www.crt:/etc/nginx/certs/certificate.crt \
 -v /etc/ca-certificates/certs/www.key:/etc/nginx/certs/certificate.key \
 kennethgrace/netgpt-webui
````

_For API:_

```bash
docker run -d -p 49488:49488 --name netgpt-api \
-v /etc/ca-certificates/certs/api.crt:/app/certs/api.crt \
-v /etc/ca-certificates/certs/api.key:/app/certs/api.key \
-v /etc/ca-certificates/certs/root.crt:/etc/ssl/certs/root.crt \
-e AUTH_SERVER=https://netgpt.example.com:7443 \
-e AUTH_CLIENT_SECRET=CHANGE_ME \
kennethgrace/netgpt-api
```

### Docker Compose

If you are using the `docker-compose.yml` file, you can provide the certificates in the `./certs` directory.
The `docker-compose.yml` file will mount the certificates into the containers. For example, the `www.crt` certificate
will be mounted into the web UI container as `/etc/nginx/certs/certificate.crt`.

### Auto-Deploy.sh

When using the `auto-deploy.sh` script, you will need to provide the certificates from the `/etc/ca-certificates/certs` directory. The script will mount the certificates into the containers.

### Generating Self-Signed Certificates

If you do not have certificates, you can generate self-signed certificates using the provided `generate.sh` script. The
script will generate a root certificate and key to generate keys and sign certificates for the application, the API, and
the authentication server.

You can run the script using the following command:

```bash
./generate.sh $COUNTRY $STATE $CITY $ORGANIZATION $ORGANIZATIONAL_UNIT $COMMON_NAME
```

Your certificates will be available in the `./certs` directory. You will be provided with the following files:

| File       | Description                     |
|------------|---------------------------------|
| `root.pem` | The root certificate.           |
| `www.crt`  | The application certificate.    |
| `www.key`  | The application private key.    |
| `api.crt`  | The API certificate.            |
| `api.key`  | The API private key.            |
| `auth.crt` | The authentication certificate. |
| `auth.key` | The authentication private key. |
| `db.crt`   | The database certificate.       |
| `db.key`   | The database private key.       |

## Configuration

Providing configuration to the application is done differently depending on whether you are configuring the web application or the API.

### API Configuration

#### Configuration File

The API can be configured via changing the configuration file located at `api/config/config.yml`. This file is a YAML file that contains the following configuration options:

| Section          | Option           | Description          | Default                  |
|------------------|------------------|----------------------|--------------------------|
| `server`         | `allowed_orgins` | The allowed origins. | `*`                      |
| `authentication` | `provider`       | The auth provider.   | `keycloak`               |
| `authentication` | `server`         | The auth URL.        | `https://localhost:7443` |
| `authentication` | `realm`          | The auth realm.      | `netgpt`                 |
| `authentication` | `client_id`      | The auth client.     | `netgpt`                 |
| `authentication` | `client_secret`  | The auth secret.     | `CHANGE_ME`              |

#### Environment Variables

The API configuration is provided using environment variables. The following environment variables are available:

| Variable             | Description                  | Default                  |
|----------------------|------------------------------|--------------------------|
| `AUTH_PROVIDER`      | The authentication provider. | `keycloak`               |
| `AUTH_SERVER`        | The authentication URL.      | `https://localhost:7443` |
| `AUTH_REALM`         | The authentication realm.    | `netgpt`                 |
| `AUTH_CLIENT_ID`     | The authentication client.   | `netgpt`                 |
| `AUTH_CLIENT_SECRET` | The authentication secret.   | `CHANGE_ME`              |
| `ALLOWED_ORGINS`     | The allowed origins.         | `*`                      |
| `CONFIG_FILE`        | The configuration file.      | `config/config.yml`      |

## Authentication

Even in a development environment, providing the program with authentication is done via OIDC. The application is designed to use KeyCloak as the authentication server. You can use any OIDC provider, but you will need to modify the API configuration to match your provider. The Web Application is currently dependent on KeyCloak. An update is planned to allow the Web Application to use any OIDC provider.

### Using a KeyCloak Server

If you plan to use KeyCloak authentication, it is always recommended to deploy an independent dedicated server. You are always responsible for the security of your environment. But if you want to run a Keycloak development server, you can do so according to the following instructions.

Firstly, you will need to provide the KeyCloak instance with certificates as well. You can provide the KeyCloak container with certificates using the same volume mounting method as for the application and API. You will also need to provide a default KeyCloak admin user and password. 

```bash
docker run -d -p 8080:8080 -p 8443:8443 --name keycloak \
 -v /etc/ca-certificates/certs/auth.crt:/etc/x509/https/tls.crt \
 -v /etc/ca-certificates/certs/auth.key:/etc/x509/https/tls.key \
 -e KEYCLOAK_ADMIN=admin -e KEYCLOAK_ADMIN_PASSWORD=CHANGE_ME \
  quay.io/keycloak/keycloak:latest start-dev --https-port=8443 \
 --https-certificate-file=/etc/x509/https/tls.crt \
 --https-certificate-key-file=/etc/x509/https/tls.key
