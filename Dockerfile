FROM python:3.9

WORKDIR /app

COPY . .

RUN pip install -r Flask Flask-Prometheus netifaces prometheus-client

EXPOSE 5000

CMD ["python", "app.py"]
