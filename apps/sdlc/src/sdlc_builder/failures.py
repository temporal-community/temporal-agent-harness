"""Public diagnostics built from known categories, never raw SDK messages or bodies."""

from temporalio.exceptions import ApplicationError, TimeoutError

from .models import TaskFailure

FAILURE_TYPE = "SdlcModelFailureV1"

# Provider error bodies sometimes echo keys, prompts, URLs, and headers. Only these
# exact codes may cross the activity boundary; all user-facing prose is ours.
KNOWN_CODES = {
    "invalid_api_key",
    "authentication_error",
    "permission_denied",
    "permission_error",
    "model_not_found",
    "not_found_error",
    "insufficient_quota",
    "billing_hard_limit_reached",
    "rate_limit_exceeded",
    "rate_limit_error",
    "context_length_exceeded",
    "request_too_large",
    "unsupported_parameter",
    "unsupported_value",
    "invalid_request_error",
    "overloaded_error",
}

RECOVER = "Choose Continue, select the corrected profile, and review the request before starting the new task."


def diagnostic(
    category: str, *, status: int | None = None, code: str = ""
) -> TaskFailure:
    messages = {
        "credential_missing": (
            "No API key is available to the worker",
            "The saved profile points to a credential that the worker could not find.",
            [
                "In Settings, add a profile with a saved API key, or set its environment variable in the shell that launches the app and restart the app.",
                RECOVER,
            ],
        ),
        "credential_store": (
            "The saved API key could not be read",
            "The operating system credential store was unavailable or denied access.",
            [
                "Unlock your OS keychain and allow the app to read its saved credential. Alternatively, add a profile using an environment variable in Settings.",
                RECOVER,
            ],
        ),
        "authentication": (
            "The provider rejected the API key",
            "Authentication failed. The key may be invalid, expired, revoked, or intended for a different provider.",
            [
                "Check the key in your provider's console, then add a profile in Settings with a valid key for the selected provider.",
                RECOVER,
            ],
        ),
        "permission": (
            "The key does not have permission for this request",
            "The provider denied access for this account, project, or model.",
            [
                "Check the key's project permissions and model access in your provider's console. Add a profile with an authorized key or model in Settings.",
                RECOVER,
            ],
        ),
        "model_not_found": (
            "The model is unavailable to this API key",
            "The provider could not find the requested model or does not grant this key access to it.",
            [
                "Copy the exact API model ID from your provider's available models. A model label from another app may not be an API model ID.",
                "In Settings, add a profile with that model and a key that can access it.",
                RECOVER,
            ],
        ),
        "not_found": (
            "The provider could not find the model or endpoint",
            "The request returned HTTP 404. This alone does not identify whether the model name, model access, or API URL is wrong.",
            [
                "Check the exact API model ID and your key's access to it. New OpenAI tasks need a model available through the Responses API.",
                "Add a corrected profile in Settings.",
                RECOVER,
            ],
        ),
        "quota": (
            "The provider's API quota is exhausted",
            "The provider reported an account billing or quota limit.",
            [
                "Check API credits, billing, and project spending limits in your provider's console, or select a funded profile.",
                "Once quota is available, choose Continue to start a new task from the preserved workspace.",
            ],
        ),
        "rate_limit": (
            "The provider is limiting requests",
            "The provider returned HTTP 429 or a rate-limit code. No explicit billing-quota code was supplied.",
            [
                "Wait before continuing and reduce other requests using this account. Check your provider's rate limits and quota if it persists.",
                "Choose Continue when ready, or select another provider profile.",
            ],
        ),
        "context_limit": (
            "The request exceeds the model's context limit",
            "The task's accumulated context is larger than this model accepts.",
            [
                "Continue with a model that supports a larger context, or narrow the request and start a fresh task.",
                "Use a smaller scope to reduce repository content sent to the model.",
            ],
        ),
        "request_rejected": (
            "The provider rejected boltzmann's request",
            "The provider rejected the request generated by the app. This response alone does not mean the model ID or API key is wrong.",
            [
                "Check the rejected parameter shown here, if available. Tool-schema and unsupported-parameter errors can require an app fix.",
                "For a compatible provider, check its API URL and tool-calling support. Report this task's provider, model, status, and code to diagnose the request before changing credentials.",
            ],
        ),
        "provider_unavailable": (
            "The provider is temporarily unavailable",
            "The provider returned a server error or an overload response.",
            [
                "Check your provider's service status and wait for recovery, or select another provider profile.",
                "Choose Continue to try again when ready.",
            ],
        ),
        "timeout": (
            "The model request timed out",
            "The worker did not receive a completed model response within the allowed time.",
            [
                "Check your connection and the provider's status. For a local provider, check that its model server is running.",
                "Choose Continue to try again, or select a faster model and narrow the request.",
            ],
        ),
        "connection": (
            "The worker could not connect to the provider",
            "The connection failed before a usable provider response arrived.",
            [
                "Check your network, VPN, proxy, and certificate settings. For a compatible provider, verify its base URL and that its server is running.",
                "Correct the connection or add a corrected profile in Settings, then Continue.",
            ],
        ),
        "invalid_response": (
            "The model did not return a usable action",
            "The response could not be validated as the structured action this agent needs.",
            [
                "Choose Continue to try again. If it repeats, select a model with reliable structured output.",
                "Inspect the task's last completed action in Harness details when reporting the issue.",
            ],
        ),
        "configuration": (
            "The model profile could not be initialized",
            "The provider adapter rejected its local configuration before completing the request.",
            [
                "In Settings, verify the provider, exact API model ID, and compatible-provider URL. Add a corrected profile.",
                RECOVER,
            ],
        ),
        "provider_error": (
            "The model request failed",
            "The provider adapter failed without a recognized error category. Its raw response is withheld because it may contain credentials or request content.",
            [
                "Review the provider and model in Settings. Open Harness details to locate the failed model call.",
                "Continue once to try again. If it repeats, report the task ID, provider, model, and status shown here.",
            ],
        ),
        "budget": (
            "The model-call budget was reached",
            "The agent used all model calls allowed by this task's profile.",
            [
                "Review the changes and checks already completed.",
                "Choose Continue with a focused follow-up, or add a profile with a larger model-call budget in Settings.",
            ],
        ),
        "action_timeout": (
            "The workspace action timed out",
            "The worker did not finish the action within its time limit. A command may have started before the timeout.",
            [
                "Inspect Changes, Checks, and Harness details to see what completed before continuing.",
                "Continue with a focused request after reviewing the workspace.",
            ],
        ),
        "action_failed": (
            "A workspace action could not finish",
            "The task stopped while processing a repository action. This does not identify a provider-settings problem.",
            [
                "Open Harness details to identify the failed action, then inspect Changes and Checks for work already completed.",
                "Check repository permissions and any required tools, then Continue with a focused follow-up.",
            ],
        ),
    }
    title, explanation, actions = messages[category]
    return TaskFailure(
        category=category,
        title=title,
        explanation=explanation,
        actions=actions,
        http_status=status,
        provider_code=code,
    )


def causes(error: BaseException):
    seen = set()
    for _ in range(12):
        if id(error) in seen:
            break
        seen.add(id(error))
        yield error
        error = error.__cause__
        if error is None:
            break


def model_failure(
    error: Exception, *, resolving_credential: bool = False
) -> TaskFailure:
    if resolving_credential:
        return diagnostic(
            "credential_missing"
            if isinstance(error, ValueError)
            else "credential_store"
        )
    chain = list(causes(error))
    for cause in chain:
        status = getattr(cause, "status_code", None)
        if type(status) is not int or not 400 <= status <= 599:
            continue
        body = getattr(cause, "body", None)
        payload = body.get("error", body) if isinstance(body, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        code = next(
            (
                value
                for value in (payload.get("code"), payload.get("type"))
                if isinstance(value, str) and value in KNOWN_CODES
            ),
            "",
        )
        if code in {"insufficient_quota", "billing_hard_limit_reached"}:
            category = "quota"
        elif status == 401:
            category = "authentication"
        elif status == 403:
            category = "permission"
        elif code == "model_not_found":
            category = "model_not_found"
        elif status == 404:
            category = "not_found"
        elif status == 429:
            category = "rate_limit"
        elif status in {408, 504}:
            category = "timeout"
        elif status >= 500 or code == "overloaded_error":
            category = "provider_unavailable"
        elif status == 413 or code in {"context_length_exceeded", "request_too_large"}:
            category = "context_limit"
        else:
            category = "request_rejected"
        result = diagnostic(category, status=status, code=code)
        # Parameter paths are also untrusted. Emit only a known root field, never
        # a server-supplied tool name, schema path, or arbitrary error message.
        param = payload.get("param")
        if category == "request_rejected" and isinstance(param, str):
            root = param.split(".", 1)[0].split("[", 1)[0]
            if root in {
                "tools",
                "tool_choice",
                "response_format",
                "messages",
                "input",
                "model",
                "temperature",
                "top_p",
                "reasoning",
                "reasoning_effort",
                "max_tokens",
                "max_completion_tokens",
                "max_output_tokens",
                "parallel_tool_calls",
                "stream",
                "store",
                "service_tier",
            }:
                result = TaskFailure(
                    **{
                        **result.model_dump(),
                        "explanation": result.explanation
                        + f" Rejected parameter: {root}.",
                    }
                )
        return result
    # These SDKs wrap transport failures. Inspect types only, never their messages.
    names = {cls.__name__ for cause in chain for cls in type(cause).__mro__}
    if names & {"TimeoutException", "APITimeoutError", "TimeoutError"}:
        category = "timeout"
    elif names & {"APIConnectionError", "TransportError", "ConnectionError"}:
        category = "connection"
    elif names & {
        "UnexpectedModelBehavior",
        "ModelRetry",
        "ModelBehaviorError",
        "MaxTurnsExceeded",
    }:
        category = "invalid_response"
    elif names & {"UserError"}:
        category = "configuration"
    else:
        category = "provider_error"
    return diagnostic(category)


def workflow_failure(error: Exception, operation: str) -> TaskFailure:
    for cause in causes(error):
        if (
            isinstance(cause, ApplicationError)
            and cause.type == FAILURE_TYPE
            and cause.details
        ):
            try:
                return TaskFailure.model_validate(cause.details[0])
            except (ValueError, TypeError):
                pass
        if isinstance(cause, TimeoutError):
            return diagnostic("timeout" if operation == "model" else "action_timeout")
    if operation == "budget":
        return diagnostic("budget")
    if operation == "model":
        return model_failure(error)
    return diagnostic("provider_error" if operation == "model" else "action_failed")
