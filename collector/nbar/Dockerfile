FROM public.ecr.aws/docker/library/golang:1.26.11-alpine3.23@sha256:6fa4e6dc2fc42920e53eb6b6a7e928155dc68282060f0901ec3cdd134c3a0eb5 AS builder
WORKDIR /src
RUN go mod download github.com/netsampler/goflow2/v2@v2.2.7 \
    && cp -R /go/pkg/mod/github.com/netsampler/goflow2/v2@v2.2.7/. . \
    && chmod u+w pkg/goflow2/builder pkg/goflow2/builder/builder.go
COPY collector/nbar/producer.go pkg/goflow2/builder/nbar.go
RUN grep -Fq 'return protoproducer.CreateProtoProducer(cfgm, protoproducer.CreateSamplingSystem)' pkg/goflow2/builder/builder.go \
    && sed -i 's/return protoproducer.CreateProtoProducer(cfgm, protoproducer.CreateSamplingSystem)/return newNBARProducer(cfgProducer, cfgm)/' pkg/goflow2/builder/builder.go \
    && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /goflow2-nbar ./cmd/goflow2

FROM scratch
COPY --from=builder /goflow2-nbar /goflow2-nbar-linux-amd64
