ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY app/ /app/
COPY run.sh /

RUN apk add --no-cache jq \
 && pip3 install --break-system-packages -r /app/requirements.txt \
 && chmod a+x /run.sh

CMD ["/run.sh"]
