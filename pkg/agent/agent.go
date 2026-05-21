package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"

	"github.com/openai/openai-go/v3"
	"github.com/openai/openai-go/v3/option"
)

type Agent struct {
	Name        string
	Description string
	Instruction string
	Model       string
	Client      openai.Client
}

func (a *Agent) Generate(ctx context.Context, prompt string) (string, error) {
	chatCompletion, err := a.Client.Chat.Completions.New(ctx, openai.ChatCompletionNewParams{
		Messages: []openai.ChatCompletionMessageParamUnion{
			openai.SystemMessage(a.Instruction),
			openai.UserMessage(prompt),
		},
		Model: openai.ChatModel(a.Model),
	})
	if err != nil {
		return "", err
	}

	if len(chatCompletion.Choices) == 0 {
		return "", fmt.Errorf("no choices returned in response")
	}

	return chatCompletion.Choices[0].Message.Content, nil
}

func (a *Agent) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		Prompt string `json:"prompt"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid JSON", http.StatusBadRequest)
		return
	}

	result, err := a.Generate(r.Context(), req.Prompt)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	resp := map[string]any{"response": result}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// NewAgentFromEnv creates an agent using environment variables.
func NewAgentFromEnv(name, description, instruction string) *Agent {
	apiBase := os.Getenv("KUBE_AGENTS_INFERENCE_BASE_URL")
	apiKey := os.Getenv("KUBE_AGENTS_INFERENCE_API_KEY")
	modelName := os.Getenv("KUBE_AGENTS_INFERENCE_MODEL")

	if apiBase == "" && apiKey == "" {
		if key := os.Getenv("GEMINI_API_KEY"); key != "" {
			apiBase = "https://generativelanguage.googleapis.com/v1beta/openai/"
			apiKey = key
			if modelName == "" {
				modelName = "gemini-3.5-flash"
			}
		}
	}

	client := openai.NewClient(
		option.WithAPIKey(apiKey),
		option.WithBaseURL(apiBase),
	)

	return &Agent{
		Name:        name,
		Description: description,
		Instruction: instruction,
		Model:       modelName,
		Client:      client,
	}
}

// StartServer starts the HTTP server for the agent.
func StartServer(a *Agent, defaultPort string) {
	port := os.Getenv("PORT")
	if port == "" {
		port = defaultPort
	}

	http.Handle("/run", a)

	fmt.Printf("Agent %s starting on port %s...\n", a.Name, port)
	if err := http.ListenAndServe(":"+port, nil); err != nil {
		log.Fatalf("Failed to start server: %v", err)
	}
}
