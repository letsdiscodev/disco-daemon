FROM python:3.12.2
ENV PYTHONUNBUFFERED 0
RUN apt-get update
RUN apt-get install -y ssh docker.io
RUN pip install uv
WORKDIR /disco/app
COPY pyproject.toml /disco/app/
COPY requirements.txt /disco/app/
ADD alembic.ini /disco/app/alembic.ini
RUN pip install -r requirements.txt
ADD disco /disco/app/disco
RUN pip install -e .
