FROM python:3.9

WORKDIR /app

COPY . .

RUN pip install -r argparse atexit json threading itertools typing netaddr.ip prometheus_client

EXPOSE 5000

CMD ["python", "app.py"]
