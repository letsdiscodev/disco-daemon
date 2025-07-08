<div align="center">
  <img src="https://github.com/letsdiscodev/.github/assets/1017304/8c1d7ecc-4bb7-411a-8da1-e7c4ff465931" alt="Disco Logo" width="150">
  <h1>Disco Daemon</h1>
  <p>
    <strong>The server-side engine for the Disco open-source PaaS.</strong>
  </p>
  <p>
    <a href="https://github.com/letsdiscodev/disco-daemon/blob/main/LICENSE"><img src="https://img.shields.io/github/license/letsdiscodev/disco-daemon?style=for-the-badge" alt="License"></a>
    <a href="https://discord.gg/7J4vb5uUwU"><img src="https://img.shields.io/discord/1200593573062651914?style=for-the-badge&logo=discord&label=discord" alt="Discord"></a>
  </p>
</div>

**Disco Daemon** is the core server-side component of the [Disco](https://disco.cloud) deployment platform. It runs on your server, acting as the brain and workhorse that manages your applications, automates deployments, and handles the underlying infrastructure.

While this repository contains the daemon's source code, you typically won't interact with it directly. Instead, you'll use the [**Disco CLI**](https://github.com/letsdiscodev/cli) to install, manage, and communicate with the daemon.

## What is Disco?

Disco is an open-source web deployment platform that lets you host web apps on your own server or Raspberry Pi with the simplicity of a managed PaaS. It helps you **Deploy Any Web App, Pay Less, and Own It All**.

The Disco ecosystem consists of two main parts:
*   [**`disco-cli`**](https://github.com/letsdiscodev/cli): The command-line interface you use on your local machine to manage your servers and projects.
*   **`disco-daemon`** (This repo): The agent that runs on your server, executing commands sent by the CLI.

## How the Daemon Works

The Disco Daemon is a self-contained system designed for reliability and ease of use. When you initialize a server with `disco init`, the CLI installs and configures this daemon for you. From then on, the daemon listens for API requests to carry out tasks.

At its core, the daemon is built on a modern, robust tech stack:

*   **FastAPI**: Exposes a clean, secure REST API for the `disco-cli` to interact with.
*   **Docker Swarm**: Manages and orchestrates your applications as containerized services, providing resilience and scalability out of the box.
*   **Caddy**: Provides an integrated, fully-managed reverse proxy with automatic HTTPS, certificate renewal, and zero-config routing for your projects.
*   **SQLAlchemy & Alembic**: Manages the persistent state of your projects, domains, and deployments in a local SQLite database, with seamless schema migrations.

### The Deployment Flow

When you deploy a project using `git push` or the CLI:

1.  **Trigger**: The daemon receives a request via a GitHub webhook or a direct API call.
2.  **Queue**: The deployment is added to a queue to be processed sequentially.
3.  **Prepare**: The daemon checks out your code, reads the `disco.json` file, and builds a Docker image.
4.  **Deploy**: A new service is started in Docker Swarm with zero downtime. Caddy automatically configures routing and TLS for any specified domains.
5.  **Cleanup**: Once the new version is healthy, the old version is gracefully shut down.

## Key Features

*   **Zero-Downtime Deployments**: Seamlessly rolls out new versions of your applications.
*   **Automatic HTTPS**: Caddy integration provides free, auto-renewing SSL/TLS certificates.
*   **Git-Based & CLI-Driven Workflows**: Deploy via a simple `git push` or `disco deploy`.
*   **Built on Docker Swarm**: Leverages a production-grade container orchestrator for stability.
*   **Extensible with Hooks**: Run pre-deployment and post-deployment scripts.
*   **Self-Contained & Lightweight**: Runs efficiently on anything from a large cloud VM to a Raspberry Pi.

## Getting Started

**You should not clone this repository to get started.**

The intended way to use Disco is through the **[Disco CLI](https://github.com/letsdiscodev/cli)**. The CLI will automatically install and manage the daemon on your server for you.

1.  **Install the Disco CLI on your local machine:**
```bash
curl https://cli-assets.letsdisco.dev/install.sh | sh
```

2.  **Initialize your server:**
    Point the CLI at your server. It will connect via SSH, install Docker, and set up the Disco Daemon.
```bash
disco init root@your-server-ip
```

From there, the CLI will guide you through connecting your GitHub account and deploying your first project.

## Development and Contribution

Interested in contributing to the Disco Daemon? That's great!

We welcome bug reports, feature requests, and pull requests. Please check out the [Issues](https://github.com/letsdiscodev/disco-daemon/issues) tab or join our [Discord](https://discord.gg/7J4vb5uUwU) to chat with the community.

## License

This project is licensed under the [MIT License](LICENSE).
