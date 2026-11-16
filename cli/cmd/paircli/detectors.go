package main

// Detector packages register themselves in init(). Keep this list complete.
import (
	_ "github.com/interviewstreet/pair-plugin/cli/internal/detect/authorship"
	_ "github.com/interviewstreet/pair-plugin/cli/internal/detect/consistency"
	_ "github.com/interviewstreet/pair-plugin/cli/internal/detect/decisions"
	_ "github.com/interviewstreet/pair-plugin/cli/internal/detect/exposure"
	_ "github.com/interviewstreet/pair-plugin/cli/internal/detect/friction"
	_ "github.com/interviewstreet/pair-plugin/cli/internal/detect/intent"
	_ "github.com/interviewstreet/pair-plugin/cli/internal/detect/oversight"
	_ "github.com/interviewstreet/pair-plugin/cli/internal/detect/verification"
)
