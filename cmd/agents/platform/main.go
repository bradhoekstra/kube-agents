package main

import (
	_ "embed"
	"github.com/gke-labs/kube-agents/pkg/agent"
)

//go:embed description.md
var description string

//go:embed instructions.md
var instructions string

func main() {
	platformAgent := agent.NewAgentFromEnv(
		"PlatformAgent",
		description,
		instructions,
	)

	agent.StartServer(platformAgent, "8080")
}


