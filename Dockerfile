FROM python:3.14-slim

WORKDIR /app
COPY pyproject.toml ./
COPY job_search ./job_search
# Editable, so the code's ROOT stays /app, where templates, fonts and the mounted data live.
RUN pip install --no-cache-dir -e .
COPY config.yaml boards.yaml ./
COPY templates ./templates
COPY fonts ./fonts

# data/, output/ and cv/ are mounted from the server, as are .env and the sheet's key, so the image holds no
# personal details and can be rebuilt freely.
EXPOSE 8000
CMD ["job-search", "serve", "--host", "0.0.0.0", "--port", "8000"]
