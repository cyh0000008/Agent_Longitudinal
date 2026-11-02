FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    BLU_AGENT_RUNTIME_DIR=/runtime \
    BLU_AGENT_RUNTIME=/runtime \
    LOCAL_BATCH_HOST=0.0.0.0 \
    LOCAL_BATCH_PORT=8080 \
    LOCAL_BATCH_UI_RUNTIME=/outputs \
    LOCAL_BATCH_DATA_ROOT=/data \
    LOCAL_BATCH_H5_SEARCH_BASE_DIR=/data \
    BATCH_ROOT_CAUSE_ANALYSIS_MODE=deepseek_investigation

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        fonts-noto-cjk \
        graphviz \
        libhdf5-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir --prefer-binary -r /app/requirements.txt

COPY *.py /app/
RUN mkdir -p /app/scripts
COPY scripts/find_j6b_h5_path.py /app/scripts/find_j6b_h5_path.py
# The intranet image uses a local-model API adapter. This override is only
# baked into this image; the normal Windows/Ubuntu Agent keeps root llm_service.py.
COPY deploy/intranet_batch/llm_service.py /app/llm_service.py
COPY analysis_engine /app/analysis_engine
COPY config /app/config
COPY database /app/database
COPY ADAS_interface_machine_readable_rules /app/ADAS_interface_machine_readable_rules

RUN mkdir -p /runtime /data /outputs \
    && useradd --create-home --uid 10001 blu \
    && chown -R blu:blu /app /runtime /data /outputs

USER blu
EXPOSE 8080

CMD ["python", "local_batch_ui.py", "--host", "0.0.0.0", "--port", "8080"]
