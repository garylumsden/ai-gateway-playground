"""Redact deployment identifiers and credentials from local execution output."""

import re


def redact(text: str, config: dict, state: dict) -> str:
    replacements = {
        config["subscriptionId"]: "<subscription-id>",
        state["hostname"]: "<gateway-host>",
        state["gatewayName"]: "<ai-gateway>",
        state["applicationInsightsId"].rsplit("/", 1)[-1]: "<new-app-insights>",
        state.get("foundryName", ""): "<foundry-account>",
        state.get("logWorkspaceName", ""): "<log-workspace>",
    }
    for value in sorted(replacements, key=len, reverse=True):
        if value:
            text = text.replace(value, replacements[value])
    text = re.sub(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", "<redacted-id>", text)
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9_.-]{20,}", "******", text)
    text = re.sub(r"(?i)InstrumentationKey=[^\s\"]+", "<redacted-connection-string>", text)
    text = re.sub(r"(?i)[A-Z]:\\Users\\[^\\\s]+", r"C:\\Users\\<operator>", text)
    text = re.sub(r"(?<![A-Za-z0-9])[A-Za-z0-9+/_=-]{64,}(?![A-Za-z0-9])", "<redacted-value>", text)
    return text
