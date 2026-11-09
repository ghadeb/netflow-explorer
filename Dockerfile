FROM ghcr.io/netsampler/goflow2:v2.2.7@sha256:b8fdc8f3666b05b0022ada3a3c8c50dcde97f7d19e3f42d050695fa0571fb952 AS decoder

FROM public.ecr.aws/docker/library/python:3.13.18-alpine3.24
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN addgroup -g 10001 fluxo && adduser -u 10001 -G fluxo -D fluxo \
    && mkdir /data /archives && chown fluxo:fluxo /data /archives
COPY collector/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=decoder /goflow2 /usr/local/bin/goflow2
COPY collector ./collector
COPY explorer /painel
COPY LICENSE /painel/LICENSE
COPY licenses/goflow2.txt /usr/share/licenses/goflow2/LICENSE

USER fluxo
CMD ["python", "-u", "/painel/start-archive.py"]
