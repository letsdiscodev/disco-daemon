FROM python:3.13.11
ENV PYTHONUNBUFFERED=0
RUN apt-get install ca-certificates curl
RUN install -m 0755 -d /etc/apt/keyrings
RUN curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
RUN chmod a+r /etc/apt/keyrings/docker.asc
RUN echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null
RUN apt-get update
RUN apt-get install -y ssh docker-ce-cli
RUN pip install uv
WORKDIR /disco/app
ADD pyproject.toml /disco/app/
ADD requirements.txt /disco/app/
ADD alembic.ini /disco/app/alembic.ini
RUN pip install -r requirements.txt
ADD disco /disco/app/disco
RUN pip install -e .
