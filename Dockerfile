FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN useradd --system --create-home --uid 10001 creator && mkdir /app/data && chown creator /app/data
COPY socialbot /app/socialbot
USER creator
ENTRYPOINT ["python", "-m", "socialbot"]
CMD ["demo", "--dashboard"]
