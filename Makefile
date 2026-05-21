LOCATION ?= us-central1
REPO ?= $(LOCATION)-docker.pkg.dev/$(shell gcloud config get core/project)/kube-agents

.PHONY: default build build-agents docker-build docker-build-agents tidy status presubmit

AGENTS := $(notdir $(wildcard cmd/agents/*))
BINS := $(foreach agent,$(AGENTS),bin/$(agent)-agent)

default: build

build: build-agents
build-agents: $(BINS)

# Proper rules for building binaries
bin/%-agent: cmd/agents/%/main.go
	@mkdir -p bin
	go build -o $@ ./cmd/agents/$*

# Docker builds
docker-build: docker-build-agents
docker-build-agents: $(foreach agent,$(AGENTS),docker-build-$(agent))

.PHONY: $(foreach agent,$(AGENTS),docker-build-$(agent))
$(foreach agent,$(AGENTS),docker-build-$(agent)): docker-build-%:
	docker build -t $(REPO)/$*-agent:latest -f cmd/agents/$*/Dockerfile .

tidy:
	go mod tidy

status:
	git status

presubmit: build docker-build tidy



