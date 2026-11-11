FROM ghcr.io/netsampler/goflow2:v2.2.7@sha256:b8fdc8f3666b05b0022ada3a3c8c50dcde97f7d19e3f42d050695fa0571fb952 AS decoder

FROM public.ecr.aws/docker/library/golang:1.26.11-alpine3.23@sha256:6fa4e6dc2fc42920e53eb6b6a7e928155dc68282060f0901ec3cdd134c3a0eb5 AS nbar-decoder
WORKDIR /src
RUN go mod download github.com/netsampler/goflow2/v2@v2.2.7 \
    && cp -R /go/pkg/mod/github.com/netsampler/goflow2/v2@v2.2.7/. . \
    && chmod u+w pkg/goflow2/builder pkg/goflow2/builder/builder.go
COPY collector/nbar/producer.go pkg/goflow2/builder/nbar.go
RUN grep -Fq 'return protoproducer.CreateProtoProducer(cfgm, protoproducer.CreateSamplingSystem)' pkg/goflow2/builder/builder.go \
    && sed -i 's/return protoproducer.CreateProtoProducer(cfgm, protoproducer.CreateSamplingSystem)/return newNBARProducer(cfgProducer, cfgm)/' pkg/goflow2/builder/builder.go \
    && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /goflow2-nbar ./cmd/goflow2

FROM public.ecr.aws/docker/library/python:3.13.18-alpine3.24
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN addgroup -g 10001 fluxo && adduser -u 10001 -G fluxo -D fluxo \
    && mkdir /data /archives && chown fluxo:fluxo /data /archives
COPY collector/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=decoder /goflow2 /usr/local/bin/goflow2
COPY --from=nbar-decoder /goflow2-nbar /usr/local/bin/goflow2-nbar
COPY collector ./collector
COPY explorer /painel
COPY LICENSE /painel/LICENSE
COPY licenses/goflow2.txt /usr/share/licenses/goflow2/LICENSE

USER fluxo
CMD ["python", "-u", "/painel/start-archive.py"]
