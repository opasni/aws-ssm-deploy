FROM python:3.12-slim

COPY requirements.txt /
RUN pip install --no-cache-dir -r /requirements.txt

COPY pipe /
COPY pipe.yml README.md LICENSE.txt /

ENTRYPOINT ["python", "/pipe.py"]
