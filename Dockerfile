FROM python:3.9

WORKDIR /app

COPY . .

RUN pip install -r Flask netifaces prometheus-client

EXPOSE 9101

CMD ["python", "main.py"]
