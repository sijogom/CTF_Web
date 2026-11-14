FROM golang:1.27.2-alpine3.24 AS builder

ENV GOTOOLCHAIN=local GOTELEMETRY=off

RUN apk add --no-cache git

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download && go list -mod=readonly -m all > /dev/null
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -mod=readonly -o /env-orchestrator ./cmd/main.go

FROM alpine:3.24.2

RUN apk --no-cache add ca-certificates curl && \
    adduser -D -u 1001 appuser
COPY --from=builder /env-orchestrator /usr/local/bin/env-orchestrator
USER appuser

EXPOSE 8200

ENTRYPOINT ["env-orchestrator"]
