package main

import (
	"fmt"
	"strings"

	"github.com/BurntSushi/toml"
)

// Agent is one [[agents]] entry of an agents.toml file. NexusEndpoint names the
// endpoint of the AgentService worker for this agent.
type Agent struct {
	Key           string `toml:"key"`
	WorkflowType  string `toml:"workflow_type"`
	TaskQueue     string `toml:"task_queue"`
	Label         string `toml:"label"`
	Description   string `toml:"description"`
	NexusEndpoint string `toml:"nexus_endpoint"`
}

// LoadAgents reads agents.toml files. It keeps only the agents that set nexus_endpoint.
func LoadAgents(paths []string) ([]Agent, error) {
	var agents []Agent
	seen := map[string]string{}
	for _, path := range paths {
		var file struct {
			Agents []Agent `toml:"agents"`
		}
		if _, err := toml.DecodeFile(path, &file); err != nil {
			return nil, fmt.Errorf("read agent registry %s: %w", path, err)
		}
		for _, agent := range file.Agents {
			if agent.NexusEndpoint == "" {
				continue
			}
			if previous, ok := seen[agent.WorkflowType]; ok {
				return nil, fmt.Errorf("agent workflow_type %q is in %s and %s", agent.WorkflowType, previous, path)
			}
			seen[agent.WorkflowType] = path
			// Match the Python registry loader: collapse the description whitespace.
			agent.Description = strings.Join(strings.Fields(agent.Description), " ")
			agents = append(agents, agent)
		}
	}
	if len(agents) == 0 {
		return nil, fmt.Errorf("no agent in %v sets nexus_endpoint", paths)
	}
	return agents, nil
}
