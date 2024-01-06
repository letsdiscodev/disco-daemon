FROM python:3.11.6
ENV PYTHONUNBUFFERED 0
RUN apt-get update
COPY setup.py /code/
COPY requirements.txt /code/
COPY requirements-types.txt /code/
WORKDIR /code
RUN pip install -r requirements.txt
RUN pip install -r requirements-types.txt
RUN python setup.py develop
