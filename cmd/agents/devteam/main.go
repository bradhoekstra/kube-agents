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
	devTeamAgent := agent.NewAgentFromEnv(
		"DevTeamAgent",
		description,
		instructions,
	)

	agent.StartServer(devTeamAgent, "8082")
}
