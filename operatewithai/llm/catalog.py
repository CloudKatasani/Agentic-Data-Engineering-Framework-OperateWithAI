"""Model providers and the agent functions that can use a model.

Same provider types and suggested models as Build-With-AI, so one team configures both
products the same way. Model lists are suggestions: every model field accepts free text and
"Discover models" asks the provider what it serves. API keys are never stored by this
application: each provider reads its key from the environment variable named here (set it
from the client's vault or secret manager).

Most of what the agents do is deliberately not a model: detection, correlation, priority,
runbook execution and SLA projection are rules, so they are predictable and auditable.
A model is used only for the language tasks listed in FUNCTIONS, each off by default, and
its output is always a draft or a suggestion that a person reviews.
"""

from __future__ import annotations

PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic (Claude)", "key_env": "ANTHROPIC_API_KEY", "needs_base_url": False, "default_base_url": "https://api.anthropic.com",
        "models": [("claude-opus-5-5", "Claude Opus 5.5 (default)"), ("claude-fable-5-2", "Claude Fable 5.2 (most capable)"),
                   ("claude-sonnet-5-5", "Claude Sonnet 5.5"), ("claude-haiku-5-5", "Claude Haiku 5.5 (fastest)")],
    },
    "openai": {
        "label": "OpenAI", "key_env": "OPENAI_API_KEY", "needs_base_url": False, "default_base_url": "https://api.openai.com/v1",
        "models": [("gpt-5.6-sol", "GPT-5.6 Sol (flagship)"), ("gpt-5.6-terra", "GPT-5.6 Terra (balanced)"), ("gpt-5.6-luna", "GPT-5.6 Luna (fastest)")],
    },
    "azure_openai": {
        "label": "Azure OpenAI", "key_env": "AZURE_OPENAI_API_KEY", "needs_base_url": True, "default_base_url": "https://<resource>.openai.azure.com",
        "options": {"api_version": "2024-10-21"}, "models": [("gpt-5-6-terra-prod", "Your deployment name (example)")],
    },
    "google_gemini": {
        "label": "Google Gemini", "key_env": "GOOGLE_API_KEY", "needs_base_url": False,
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "models": [("gemini-4-argon", "Gemini 4 Argon"), ("gemini-3.8-flash", "Gemini 3.8 Flash"), ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite")],
    },
    "aws_bedrock": {
        "label": "Amazon Bedrock", "key_env": "AWS_BEARER_TOKEN_BEDROCK", "needs_base_url": False, "options": {"region": "us-east-1"},
        "models": [("anthropic.claude-opus-5-5", "Claude Opus 5.5 on Bedrock"), ("anthropic.claude-sonnet-5-5", "Claude Sonnet 5.5 on Bedrock"),
                   ("anthropic.claude-haiku-5-5", "Claude Haiku 5.5 on Bedrock")],
    },
    "openai_compatible": {
        "label": "OpenAI-compatible endpoint (Ollama, vLLM, gateway)", "key_env": "OWAI_COMPATIBLE_API_KEY", "key_optional": True, "needs_base_url": True,
        "default_base_url": "http://localhost:11434/v1",
        "models": [("llama3.3", "Llama 3.3 (Ollama)"), ("qwen2.5-coder", "Qwen 2.5 Coder (Ollama)"), ("mistral-large-latest", "Mistral Large (Mistral API)")],
    },
}

# The language tasks a model can take on, by agent. "guard" is what keeps the output safe to use.
FUNCTIONS: list[dict] = [
    {"id": "triage.route_hint", "agent": "Triage Agent", "title": "Suggest the owning team for an unrecognised alert",
     "does": "When no known error matches, reads the alert text and suggests the likely cause area and tower.",
     "guard": "Shown on the ticket as a suggestion. Routing still follows the system's owner.", "suggest": ("anthropic", "claude-haiku-5-5")},
    {"id": "diagnosis.hypotheses", "agent": "Diagnosis Agent", "title": "Hypotheses for an unknown error",
     "does": "When no known error matches or the checks disagree, proposes up to three likely causes and read-only checks to run next.",
     "guard": "Labelled unverified. It never becomes the diagnosis and cannot select an action.", "suggest": ("anthropic", "claude-opus-5-5")},
    {"id": "comms.rewrite", "agent": "Comms Agent", "title": "Write the stakeholder message in plain business language",
     "does": "Rewrites the drafted incident message for a business audience, keeping every fact, time and name.",
     "guard": "The Incident Manager approves or edits every message before it is sent. Falls back to the template.", "suggest": ("anthropic", "claude-sonnet-5-5")},
    {"id": "knowledge.review_summary", "agent": "Problem and Knowledge Agent", "title": "Executive summary of the post-incident review",
     "does": "Adds a short summary for management at the top of the review, written from the ticket record.",
     "guard": "The Service Delivery Manager approves the review.", "suggest": ("anthropic", "claude-sonnet-5-5")},
    {"id": "knowledge.known_error", "agent": "Problem and Knowledge Agent", "title": "Draft a known error from the engineer's notes",
     "does": "Turns the engineer's resolution notes into a clear cause and fix description for the known error database.",
     "guard": "The resolving engineer approves before it is added.", "suggest": ("anthropic", "claude-sonnet-5-5")},
    {"id": "reporting.handover_summary", "agent": "Service Reporting Agent", "title": "Summary paragraph for the shift handover",
     "does": "Writes the opening summary of the handover from the open incidents, waiting decisions and service outlook.",
     "guard": "The facts below the summary are generated from the record and are unchanged.", "suggest": ("anthropic", "claude-haiku-5-5")},
]

RULES_ONLY = [
    ("Watch Desk Agent", "Parsing, de-duplication and suppression are rules: the same input always gives the same alert."),
    ("Resolution Agent", "Carries out approved runbook steps from the action catalogue. A model never chooses or runs an action."),
    ("Batch and SLA Agent", "Delivery estimates are calculated from scheduler state and the data chain."),
    ("Service Request Agent", "Approval rules come from the request catalogue."),
]
