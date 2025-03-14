FROM python:3.12.9
ENV PYTHONUNBUFFERED=0
RUN apt-get update
RUN apt-get install -y ssh docker.io
RUN pip install uv
WORKDIR /disco/app
ADD pyproject.toml /disco/app/
ADD requirements.txt /disco/app/
ADD alembic.ini /disco/app/alembic.ini
RUN pip install -r requirements.txt
ADD disco /disco/app/disco
RUN pip install -e .
