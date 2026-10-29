package presentation

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestClip(t *testing.T) {
	assert.Equal(t, "abc", Clip("abc", 3))
	assert.Equal(t, "ab…", Clip("abcd", 3))
	assert.Equal(t, "é…", Clip("ééé", 2))
}

func TestHumanize(t *testing.T) {
	assert.Equal(t, "New Policy", Humanize("new_policy"))
	assert.Equal(t, "Dangerously Skip All", Humanize("dangerously_skip_all"))
	assert.Equal(t, "Max Tokens", Humanize("maxTokens"))
	assert.Equal(t, "A B", Humanize("a-b"))
}
