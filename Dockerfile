FROM python:3.12.1
ENV PYTHONUNBUFFERED 0
RUN apt-get update
RUN apt-get install -y ssh docker.io
COPY setup.py /disco/app/
COPY requirements.txt /disco/app/
COPY requirements-types.txt /disco/app/
ADD alembic.ini /disco/app/alembic.ini
ADD disco /disco/app/disco
WORKDIR /disco/app
RUN pip install -r requirements.txt
RUN pip install -r requirements-types.txt
