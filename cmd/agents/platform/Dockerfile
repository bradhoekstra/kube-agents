# Build stage
FROM golang:1.26.3 AS builder

WORKDIR /app

# Copy go.mod and go.sum
# Assumes build context is the root of the repository
COPY go.mod go.sum ./
RUN go mod download

# Copy the source code
COPY . .

# Build the application
RUN CGO_ENABLED=0 GOOS=linux go build -o platform-agent ./cmd/agents/platform/main.go

# Final stage
FROM gcr.io/distroless/static-debian12

WORKDIR /

COPY --from=builder /app/platform-agent /platform-agent

ENTRYPOINT ["/platform-agent"]
