FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --disable-pip-version-check -r requirements.txt
COPY app.py generation.py inspector.py library.py prompt_composer.py provider_settings.py recipe_repair.py ./
COPY static/ ./static/
EXPOSE 8724
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8724/api/health',timeout=3))['app']=='nai-style-library'"
CMD ["python", "-B", "app.py", "--host", "0.0.0.0", "--data", "/app/data"]

FROM runtime AS test
COPY test_generation.py test_reroll.py test_provider_settings.py test_recipe_repair.py ./
RUN python -B -m unittest test_generation test_reroll test_provider_settings test_recipe_repair

FROM runtime AS production
