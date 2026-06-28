FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser (separate layer — re-runs only when
# the playwright package version in requirements.txt changes).
# --with-deps also installs the necessary system libraries (libnss3, etc.).
RUN playwright install --with-deps chromium

# Copy test source code
COPY pdf_generator.py .
COPY file_dropper.py .
COPY helpers.py .
COPY tester_logger.py .
COPY conftest.py .
COPY pytest.ini .
COPY web_runner.py .
COPY entrypoint.sh .
COPY tests/ ./tests/

RUN chmod +x entrypoint.sh

# /data is mounted at runtime (shared with pdf-dispatch-test)
VOLUME ["/data"]

# Inject Git SHA at build time — accessible via os.environ['APP_VERSION'].
# Set by GitHub Actions: --build-arg GIT_SHA=${{ github.sha }}
ARG GIT_SHA=unknown
ENV APP_VERSION=${GIT_SHA}

ENTRYPOINT ["./entrypoint.sh"]
