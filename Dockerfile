FROM python:3.12.1
ENV PYTHONUNBUFFERED 0
RUN apt-get update
RUN apt-get install -y ssh docker.io
COPY setup.py /code/
COPY requirements.txt /code/
COPY requirements-types.txt /code/
ADD disco /code/disco
WORKDIR /code
RUN pip install -r requirements.txt
RUN pip install -r requirements-types.txt
RUN python setup.py develop
