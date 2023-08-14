# NetGPT: The Network Engineering GPT

## Introduction

NetGPT is a Web App and API for performing network engineering tasks using GPT natural language processing. It is built using [FastAPI](https://fastapi.tiangolo.com/) and [React](https://reactjs.org/). The Application and
the API are containerized using [Docker](https://www.docker.com/).

## Installation

### Docker Hub

The easiest way to run NetGPT is using Docker. The Docker images are available on [Docker Hub](https://hub.docker.com/u/kennethgrace). The application is available as `kennethgrace/netgpt-webui` and the API is available as `kennethgrace/netgpt-api`. The images are built automatically from the `main` branch of this repository. To run the application, use the following command:

```bash
docker run -d -p 80:80 kennethgrace/netgpt-webui
```

To run the API, use the following command:

```bash
docker run -d -p 49488:49488 kennethgrace/netgpt-api
```

### Docker Compose

If you want to run both the application and the API, you can use the included `docker-compose.yml` file. To run the application and the API, use the following command:

```bash
git clone https://github.com/kennethgrace/netgpt.git
cd netgpt
docker compose up -d
```
