# NetGPT: The Network Engineering GPT

## Introduction

NetGPT is a Web App and API for performing network engineering tasks using GPT natural language processing. It is built
using [FastAPI](https://fastapi.tiangolo.com/) and [React](https://reactjs.org/). The Application and
the API are containerized using [Docker](https://www.docker.com/).

## Installation

### Docker Containers

The easiest way to run NetGPT is using Docker. The Docker images are available
on [Docker Hub](https://hub.docker.com/u/kennethgrace). The application is available as `kennethgrace/netgpt-webui` and
the API is available as `kennethgrace/netgpt-api`. The images are built automatically from the `main` branch of this
repository. To run the application, use the following command:

```bash
docker run -d -p 80:8080 -p 443:8443 kennethgrace/netgpt-webui
```

To run the API, use the following command:

```bash
docker run -d -p 49488:49488 kennethgrace/netgpt-api
```

The application will be available at `https://localhost` and the API will be available at `https://localhost:49488`.
This is the default server url for the application, if you have separate servers for the application and the API, you
can change the server url in the application settings of the web UI.

### Docker Compose

If you want to run both the application and the API, you can use the included `docker-compose.yml` file. To run the
application and the API, use the following command:

```bash
git clone https://github.com/kennethgrace/netgpt.git
cd netgpt
docker compose up -d
```

## Certificates

The application uses certificates for HTTPS. Whether you plan to use the application in production or not, you will need
to provide certificates. If you do not provide certificates, the application will
not function properly.

### Docker Containers

When using Docker, you can provide the certificates using the following commands:

_For Web UI:_

```bash
docker run -d -p 80:8080 -p 443:8443 \
 -v /etc/ca-certificates/certs/www.crt:/etc/nginx/certs/certificate.crt \
 -v /etc/ca-certificates/certs/www.key:/etc/nginx/certs/certificate.key \
 kennethgrace/netgpt-webui
```

_For API:_

```bash
docker run -d -p 49488:49488 \
 -v /etc/ca-certificates/certs/api.crt:/app/certs/api.crt \
 -v /etc/ca-certificates/certs/api.key:/app/certs/api.key \
 kennethgrace/netgpt-api
```

#### Using KeyCloak

If you plan to use the KeyCloak authentication, you will need to provide the KeyCloak certificate as well. You can
provide the KeyCloak container with certificates using the following command:

```bash
docker run -d -p 7080:8080 -p 7443:8443 \
 -v /etc/ca-certificates/certs/auth.crt:/etc/x509/https/tls.crt \
 -v /etc/ca-certificates/certs/auth.key:/etc/x509/https/tls.key \
 quay.io/keycloak/keycloak:latest
```

### Docker Compose

If you are using the `docker-compose.yml` file, you can provide the certificates in the `certs` directory.
The `docker-compose.yml` file will mount the certificates into the containers. For example, the `www.crt` certificate
will be mounted into the web UI container as `/etc/nginx/certs/certificate.crt`.

### Generating Self-Signed Certificates

If you do not have certificates, you can generate self-signed certificates using the provided `generate.sh` script. The
script will generate a root certificate and key to generate keys and sign certificates for the application, the API, and
the authentication server.

You can run the script using the following command:

```bash
./generate.sh $COUNTRY $STATE $CITY $ORGANIZATION $COMMON_NAME
```

Your certificates will be available in the `certs` directory. You will be provided with the following files:

| File       | Description                     |
|------------|---------------------------------|
| `root.pem` | The root certificate.           |
| `www.crt`  | The application certificate.    |
| `www.key`  | The application private key.    |
| `api.crt`  | The API certificate.            |
| `api.key`  | The API private key.            |
| `auth.crt` | The authentication certificate. |
| `auth.key` | The authentication private key. |

## Configuration