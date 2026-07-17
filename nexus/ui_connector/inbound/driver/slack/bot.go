package slack

import (
	slackapi "github.com/slack-go/slack"
)

// SlackBot holds authenticated Slack credentials.
type SlackBot struct {
	Client *slackapi.Client
	TeamID string
	UserID string
}

// NewSlackBot authenticates with Slack and returns a SlackBot.
func NewSlackBot(token string) (*SlackBot, error) {
	c := slackapi.New(token)
	authTest, err := c.AuthTest()
	if err != nil {
		return nil, err
	}
	return &SlackBot{
		Client: c,
		TeamID: authTest.TeamID,
		UserID: authTest.UserID,
	}, nil
}
