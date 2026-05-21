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
	operatorAgent := agent.NewAgentFromEnv(
		"OperatorAgent",
		description,
		instructions,
	)

	agent.StartServer(operatorAgent, "8081")
}
