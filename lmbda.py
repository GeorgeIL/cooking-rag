import os
import logging
import json
import re
import urllib.request
import urllib.parse
from typing import Dict, Any, Optional
from http import HTTPStatus
from datetime import datetime

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
_BEDROCK_KB_ID = os.environ.get("BEDROCK_KB_ID", "")
_S3_BUCKET = os.environ.get("S3_BUCKET_NAME", "")
_S3_PREFIX = os.environ.get("S3_RECIPES_PREFIX", "recipes/")
_CATALOG_MANIFEST_KEY = f"{_S3_PREFIX}catalog/manifest.json"
_METEOSOURCE_API_KEY = os.environ.get("METEOSOURCE_API_KEY", "")
_FLASK_TOOL_URL = os.environ.get(
    "FLASK_TOOL_URL", "http://127.0.0.1:5000/chat/agent/share-recipe"
)
_AGENT_TOOL_SECRET = os.environ.get("AGENT_TOOL_SECRET", "")
_BUDDY_EMAIL_LAMBDA = os.environ.get(
    "BUDDY_EMAIL_LAMBDA_NAME", "cooking-rag-buddy-email"
)

_lambda_client = None

_RECIPE_KEYWORDS = (
    "recipe",
    "cook",
    "dish",
    "meal",
    "suggest",
    "recommend",
    "dinner",
    "lunch",
    "breakfast",
    "brunch",
    "supper",
    "eat",
    "food",
    "make for",
)

_bedrock_agent_runtime = None
_s3_client = None
_manifest_cache: dict[str, dict] | None = None

_CATALOG_ID_HEADING_RE = re.compile(r"^\d+\s+\*\*Tags:\*\*", re.IGNORECASE)


def _agent_runtime():
    global _bedrock_agent_runtime
    if _bedrock_agent_runtime is None:
        _bedrock_agent_runtime = boto3.client(
            "bedrock-agent-runtime", region_name=_AWS_REGION
        )
    return _bedrock_agent_runtime


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=_AWS_REGION)
    return _lambda_client


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=_AWS_REGION)
    return _s3_client


def _title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").strip().title()


def _load_catalog_manifest() -> dict[str, dict]:
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache

    lookup: dict[str, dict] = {}
    if not _S3_BUCKET:
        _manifest_cache = lookup
        return lookup

    try:
        response = _s3().get_object(Bucket=_S3_BUCKET, Key=_CATALOG_MANIFEST_KEY)
        payload = json.loads(response["Body"].read().decode("utf-8"))
        for item in payload.get("recipes", []):
            slug = str(item.get("slug") or "").strip()
            if slug:
                lookup[slug] = item
    except Exception as exc:
        logger.warning("Could not load catalog manifest: %s", exc)

    _manifest_cache = lookup
    return lookup


def _slug_from_s3_uri(uri: str) -> str | None:
    if not uri:
        return None
    path = uri.split("://", 1)[-1]
    if "/" in path:
        path = path.split("/", 1)[1]
    if not path.endswith(".md"):
        return None
    slug = path.rsplit("/", 1)[-1][:-3]
    if slug == "manifest":
        return None
    return slug or None


def _s3_key_from_uri(uri: str) -> str | None:
    if not uri or "://" not in uri:
        return None
    return uri.split("://", 1)[1].split("/", 1)[-1]


def _title_from_manifest_slug(slug: str, manifest: dict[str, dict]) -> str | None:
    item = manifest.get(slug)
    if item:
        title = str(item.get("title") or "").strip()
        if title:
            return title
    return None


def _title_from_s3_key(s3_key: str) -> str | None:
    if not _S3_BUCKET or not s3_key:
        return None
    try:
        body = _s3().get_object(Bucket=_S3_BUCKET, Key=s3_key)["Body"].read().decode(
            "utf-8"
        )
    except Exception:
        return None
    return _title_from_md_heading(body)


def _title_from_json_snippet(text: str) -> str | None:
    match = re.search(r'"title"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return match.group(1).replace('\\"', '"').strip() or None


def _title_from_md_heading(text: str) -> str | None:
    match = re.search(r"^#\s+(.+)$", text.strip(), re.MULTILINE)
    if not match:
        return None
    title = match.group(1).strip()
    if not title or title.isdigit() or _CATALOG_ID_HEADING_RE.match(title):
        return None
    if "##" in title or "**Tags:**" in title:
        return None
    return title


def _resolve_recipe_title(text: str, source_uri: str, manifest: dict[str, dict]) -> str | None:
    slug = _slug_from_s3_uri(source_uri)
    if slug:
        title = _title_from_manifest_slug(slug, manifest)
        if title:
            return title

    title = _title_from_json_snippet(text)
    if title:
        return title

    title = _title_from_md_heading(text)
    if title:
        return title

    if slug:
        if not slug.isdigit():
            return _title_from_slug(slug)
        s3_key = _s3_key_from_uri(source_uri)
        if s3_key:
            return _title_from_s3_key(s3_key)

    return None


def _prompt_attr(event: Dict[str, Any], name: str) -> str:
    attrs = event.get("promptSessionAttributes") or {}
    return str(attrs.get(name) or "").strip()


def _parse_buddy_contacts(session_attrs: Dict[str, Any]) -> dict[str, str]:
    raw = str(session_attrs.get("buddy_contacts") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except json.JSONDecodeError:
        logger.warning("Invalid buddy_contacts JSON in sessionAttributes")
    return {}


def _resolve_buddy_contact(
    buddy_name: str, contacts: dict[str, str]
) -> tuple[str, str] | None:
    if not buddy_name or not contacts:
        return None

    normalized = re.sub(r"\s+", " ", buddy_name.strip().lower())
    for name, email in contacts.items():
        if name.strip().lower() == normalized:
            return name, email

    for name, email in sorted(contacts.items(), key=lambda item: len(item[0]), reverse=True):
        name_lower = name.strip().lower()
        if normalized in name_lower or name_lower.startswith(f"{normalized} "):
            return name, email

    first = normalized.split()[0] if normalized else ""
    if first:
        matches = [
            (name, email)
            for name, email in contacts.items()
            if name.strip().lower().split()[0] == first
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def _build_email_context(recipe_title: str, recipe_body: str) -> str:
    title = recipe_title.strip()
    body = recipe_body.strip()
    if body.lower().startswith("#"):
        return body
    return f"# {title}\n\n{body}"


def _invoke_buddy_email_lambda(
    *,
    sender_name: str,
    buddy_name: str,
    buddy_email: str,
    recipe_title: str,
    recipe_body: str,
) -> str:
    if not _BUDDY_EMAIL_LAMBDA:
        raise ValueError("BUDDY_EMAIL_LAMBDA_NAME is not configured on the action Lambda")

    context = _build_email_context(recipe_title, recipe_body)
    payload = {
        "sender_name": sender_name or "A Smart Cookbook user",
        "subject": f"Recipe: {recipe_title.strip()}",
        "context": context,
        "buddies": [{"name": buddy_name, "email": buddy_email}],
    }
    _lambda().invoke(
        FunctionName=_BUDDY_EMAIL_LAMBDA,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    return f"Email queued for {buddy_name} ({buddy_email})"


def _share_via_flask(
    *,
    user_id: str,
    sender_name: str,
    buddy_name: str,
    recipe_title: str,
    recipe_body: str,
) -> str:
    if not _AGENT_TOOL_SECRET:
        raise ValueError("AGENT_TOOL_SECRET is not configured on the Lambda")

    payload = json.dumps(
        {
            "user_id": user_id,
            "sender_name": sender_name,
            "buddy_name": buddy_name,
            "recipe_title": recipe_title,
            "recipe_body": recipe_body,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        _FLASK_TOOL_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Agent-Tool-Secret": _AGENT_TOOL_SECRET,
        },
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        if resp.status != 200 or not body.get("ok"):
            raise Exception(body.get("error") or body.get("message") or "Share failed")
        return str(body.get("message") or "Email queued successfully.")


def _param_value(event: Dict[str, Any], name: str) -> Optional[str]:
    for param in event.get("parameters", []):
        if param.get("name", "").lower() == name.lower():
            return param.get("value")
    return None


def _input_text(event: Dict[str, Any]) -> str:
    return str(event.get("inputText") or "").strip()


def _normalize_place_id(location: str) -> str:
    return location.strip().lower().replace(" ", "-")


def _wants_recipe_suggestion(text: str) -> bool:
    lower = text.lower()
    return any(keyword in lower for keyword in _RECIPE_KEYWORDS)


_LOCATION_STOPWORDS = {
    "the",
    "a",
    "an",
    "my",
    "this",
    "that",
    "current",
    "today",
    "now",
    "me",
    "you",
    "right",
}


def _resolve_location(event: Dict[str, Any]) -> Optional[str]:
    param_location = _param_value(event, "location")
    if param_location:
        return _normalize_place_id(param_location)

    text = _input_text(event)
    if not text:
        return None

    in_matches = re.findall(
        r"\bin\s+([A-Za-z][A-Za-z\s\-]{1,40}?)(?:\?|\.|,|$|\s+(?:right\s+now|currently|today|weather|time))",
        text,
        re.IGNORECASE,
    )
    for place in reversed(in_matches):
        cleaned = place.strip()
        if cleaned.lower() not in _LOCATION_STOPWORDS:
            return _normalize_place_id(cleaned)

    for_match = re.search(
        r"\bfor\s+([A-Z][a-z]+(?:[\s\-][A-Z][a-z]+)*)(?:\?|\.|,|$|\s)",
        text,
    )
    if for_match:
        return _normalize_place_id(for_match.group(1))

    fallback_patterns = [
        r"\b([A-Za-z][A-Za-z\s\-]{0,40}?)\s+weather\b",
        r"\bweather\s+in\s+([A-Za-z][A-Za-z\s\-]{0,40}?)(?:\?|\.|,|$)",
    ]
    for pattern in fallback_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            place = match.group(1).strip()
            if place.lower() not in _LOCATION_STOPWORDS:
                return _normalize_place_id(place)
    return None


def _meal_period(hour: int) -> tuple[str, str]:
    if hour < 11:
        return "morning", "breakfast"
    if hour < 16:
        return "afternoon", "lunch"
    return "evening", "dinner"


def _fetch_weather(location: str) -> tuple[str, str]:
    if not _METEOSOURCE_API_KEY:
        raise ValueError("METEOSOURCE_API_KEY is not configured")

    url = "https://www.meteosource.com/api/v1/free/point"
    params = {
        "place_id": location,
        "sections": "current",
        "timezone": "UTC",
        "language": "en",
        "units": "metric",
        "key": _METEOSOURCE_API_KEY,
    }
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"

    req = urllib.request.Request(full_url, method="GET")
    with urllib.request.urlopen(req, timeout=15) as api_response:
        if api_response.status != 200:
            raise Exception(f"Weather API failed with status: {api_response.status}")

        weather_data = json.loads(api_response.read().decode("utf-8"))
        current = weather_data.get("current", {})
        temp = current.get("temperature", "unknown")
        summary = current.get("summary", "no data")
        return str(summary), str(temp)


def _retrieve_recipe_suggestions(query: str, top_k: int = 12) -> list[dict[str, str]]:
    """Retrieve KB hits with source URI so we can resolve real recipe titles."""
    if not _BEDROCK_KB_ID:
        return []

    try:
        response = _agent_runtime().retrieve(
            knowledgeBaseId=_BEDROCK_KB_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": top_k}
            },
        )
    except Exception as exc:
        logger.error("Knowledge base retrieve failed: %s", exc)
        return []

    hits: list[dict[str, str]] = []
    for result in response.get("retrievalResults", []):
        text = (result.get("content") or {}).get("text", "").strip()
        if not text:
            continue
        location = result.get("location") or {}
        s3_loc = location.get("s3Location") or {}
        source_uri = str(s3_loc.get("uri") or "").strip()
        if source_uri.endswith("/manifest.json"):
            continue
        hits.append({"text": text, "source_uri": source_uri})
    return hits


def _extract_titles(hits: list[dict[str, str]], limit: int = 3) -> list[str]:
    manifest = _load_catalog_manifest()
    titles: list[str] = []
    seen_sources: set[str] = set()

    for hit in hits:
        source_uri = hit.get("source_uri") or hit.get("text", "")[:80]
        if source_uri in seen_sources:
            continue

        title = _resolve_recipe_title(hit.get("text", ""), hit.get("source_uri", ""), manifest)
        if not title:
            continue

        seen_sources.add(source_uri)
        if title not in titles:
            titles.append(title)
        if len(titles) >= limit:
            break

    return titles


def _get_time_text() -> str:
    now = datetime.now()
    return f"The current date and time is {now.strftime('%Y-%m-%d %H:%M:%S')}"


def _get_weather_text(location: str) -> str:
    summary, temp = _fetch_weather(location)
    return f"The weather in {location} is {summary} with temperature {temp}°C."


def _suggest_dish_for_time_and_weather(event: Dict[str, Any]) -> str:
    location = _resolve_location(event)
    meal_hint = (_param_value(event, "meal_hint") or "").strip()

    # Weather/location are OPTIONAL context here — never a hard requirement. If the
    # agent passes a missing or placeholder location (e.g. "unknown"), or the
    # weather API fails, we MUST still return a useful suggestion. An errored tool
    # response can poison the agent session and make it apologise repeatedly, so
    # this function is designed to never raise.
    if location and location.strip().lower() in ("unknown", "none", "n/a", ""):
        location = None

    now = datetime.now()
    period_name, meal_type = _meal_period(now.hour)

    summary = temp = None
    if location:
        try:
            summary, temp = _fetch_weather(location)
        except Exception as exc:  # noqa: BLE001 — weather is best-effort context
            logger.warning(
                "Weather lookup failed for %r (continuing without it): %s",
                location,
                exc,
            )
            summary = temp = None

    query_parts = [
        "recipes for",
        summary or "",
        f"{temp}C" if temp is not None else "",
        period_name,
        meal_type,
        meal_hint,
    ]
    retrieval_query = " ".join(part for part in query_parts if part)
    hits = _retrieve_recipe_suggestions(retrieval_query)
    titles = _extract_titles(hits)

    lines = [
        f"Current date and time: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Meal period: {period_name} ({meal_type})",
    ]
    if location and summary is not None:
        lines.append(f"Weather in {location}: {summary}, {temp}°C.")
    else:
        lines.append(
            "Weather: unavailable — ignore weather and suggest based on the "
            "pantry, meal preference, and time of day."
        )
    if meal_hint:
        lines.append(f"Meal preference / pantry: {meal_hint}.")

    if titles:
        lines.append("Suggested cookbook recipes (names only — use KB for full details):")
        for idx, title in enumerate(titles, start=1):
            lines.append(f"{idx}. {title}")
    else:
        lines.append(
            "No matching cookbook recipes for this context; answer from the "
            "pantry and general culinary knowledge."
        )

    return "\n".join(lines)


def _handle_get_time(event: Dict[str, Any]) -> str:
    text = _input_text(event)
    location = _resolve_location(event)
    wants_recipe = _wants_recipe_suggestion(text) or bool(_param_value(event, "meal_hint"))

    if wants_recipe or location:
        if location and _METEOSOURCE_API_KEY:
            if _BEDROCK_KB_ID:
                return _suggest_dish_for_time_and_weather(event)
            lines = [_get_time_text(), _get_weather_text(location)]
            lines.append(
                "Recipe suggestions unavailable: BEDROCK_KB_ID is not configured on the Lambda."
            )
            return "\n".join(lines)
        if wants_recipe and not location:
            return (
                f"{_get_time_text()}\n\n"
                "To suggest recipes, please specify a city (e.g. paris, london, tel-aviv)."
            )

    return _get_time_text()


def _handle_get_weather(event: Dict[str, Any]) -> str:
    location = _resolve_location(event)
    if not location:
        raise ValueError(
            "Location parameter is required (or mention a city in the request)."
        )

    text = _input_text(event)
    wants_recipe = _wants_recipe_suggestion(text) or bool(_BEDROCK_KB_ID)

    if wants_recipe and _BEDROCK_KB_ID and _METEOSOURCE_API_KEY:
        return _suggest_dish_for_time_and_weather(event)

    return _get_weather_text(location)


def _share_recipe_with_buddy(event: Dict[str, Any]) -> str:
    buddy_name = (_param_value(event, "buddy_name") or "").strip()
    recipe_title = (_param_value(event, "recipe_title") or "").strip()
    recipe_body = (_param_value(event, "recipe_body") or "").strip()

    if not recipe_title:
        recipe_title = _prompt_attr(event, "last_recipe_title")
    if not recipe_body:
        recipe_body = _prompt_attr(event, "last_recipe_body")

    if not buddy_name:
        raise ValueError("buddy_name parameter is required")
    if not recipe_title:
        raise ValueError(
            "recipe_title is required (pass it or ensure last_recipe_title is in session)"
        )
    if not recipe_body:
        raise ValueError(
            "recipe_body is required (pass it or ensure last_recipe_body is in session)"
        )

    session_attrs = event.get("sessionAttributes") or {}
    user_id = (session_attrs.get("user_id") or "").strip()
    sender_name = (session_attrs.get("sender_name") or "A Smart Cookbook user").strip()

    if not user_id:
        raise ValueError("user_id missing from sessionAttributes")

    contacts = _parse_buddy_contacts(session_attrs)
    resolved = _resolve_buddy_contact(buddy_name, contacts)
    if resolved:
        canonical_name, buddy_email = resolved
        try:
            return _invoke_buddy_email_lambda(
                sender_name=sender_name,
                buddy_name=canonical_name,
                buddy_email=buddy_email,
                recipe_title=recipe_title,
                recipe_body=recipe_body,
            )
        except Exception as exc:
            logger.error("Direct buddy email invoke failed: %s", exc)
            if "AccessDenied" in str(exc) or "not authorized" in str(exc).lower():
                raise Exception(
                    "Lambda lacks permission to invoke the buddy email function. "
                    "Add lambda:InvokeFunction on cooking-rag-buddy-email to the action Lambda role."
                ) from exc
            raise

    # Fallback: Flask resolves buddy in RDS (requires reachable FLASK_TOOL_URL).
    try:
        return _share_via_flask(
            user_id=user_id,
            sender_name=sender_name,
            buddy_name=buddy_name,
            recipe_title=recipe_title,
            recipe_body=recipe_body,
        )
    except urllib.error.URLError as exc:
        raise Exception(
            "Could not reach Flask to send the email. Configure buddy_contacts in "
            "sessionAttributes or set BUDDY_EMAIL_LAMBDA_NAME on the action Lambda."
        ) from exc
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            message = parsed.get("error") or parsed.get("message") or detail
        except json.JSONDecodeError:
            message = detail or str(exc)
        raise Exception(message) from exc


def _action_response(
    action_group: str,
    function: str,
    text: str,
    message_version: str,
) -> Dict[str, Any]:
    return {
        "response": {
            "actionGroup": action_group,
            "function": function,
            "functionResponse": {
                "responseBody": {"TEXT": {"body": str(text)}},
                "httpStatusCode": 200,
            },
        },
        "messageVersion": str(message_version),
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda handler for processing Bedrock agent requests.
    """
    action_group = event.get("actionGroup", "")
    function = event.get("function", "")
    message_version = event.get("messageVersion", "1.0")

    try:
        logger.info(
            "Action request function=%s inputText=%r parameters=%s",
            function,
            _input_text(event),
            event.get("parameters"),
        )

        if function in (
            "SuggestDishForTimeAndWeather",
            "GetWeather",
            "GetTime",
        ):
            if function == "GetTime":
                text = _handle_get_time(event)
            elif function == "GetWeather":
                text = _handle_get_weather(event)
            else:
                text = _suggest_dish_for_time_and_weather(event)
        elif function == "ShareRecipeWithBuddy":
            text = _share_recipe_with_buddy(event)
        else:
            text = f"Unknown function: {function}"

        response = _action_response(action_group, function, text, message_version)
        logger.info("Response sent to Bedrock: %s", response)
        return response

    except KeyError as e:
        logger.error("Missing required field: %s", str(e))
        return {
            "statusCode": HTTPStatus.BAD_REQUEST,
            "body": f"Error: {str(e)}",
        }
    except Exception as e:
        logger.error("Unexpected error: %s", str(e))
        logger.exception(e)
        error_text = f"Tool error: {str(e)}"
        if action_group and function:
            return _action_response(action_group, function, error_text, message_version)
        return {
            "statusCode": HTTPStatus.INTERNAL_SERVER_ERROR,
            "body": f"Internal server error: {str(e)}",
        }
