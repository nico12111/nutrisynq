FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY . .

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "src.main"]
CMD ["run", "--dry-run"]
