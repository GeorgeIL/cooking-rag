"""
Lambda handler: compose personalized emails with Bedrock Nova and send via SES.

Expected event payload (from Flask /buddies/send):
{
  "sender_name": "alice",
  "subject": "A recipe from Smart Cookbook",
  "context": "Recipe: Apple Pie\\n\\n...",
  "buddies": [{"name": "Sarah", "email": "sarah@example.com"}, ...]
}
"""

from __future__ import annotations

import json
import os

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
NOVA_MODEL_ID = os.environ.get("NOVA_MODEL_ID", "amazon.nova-lite-v1:0")
SES_FROM_EMAIL = os.environ.get("SES_FROM_EMAIL", "")

_bedrock = None
_ses = None


def _bedrock_client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock


def _ses_client():
    global _ses
    if _ses is None:
        _ses = boto3.client("ses", region_name=AWS_REGION)
    return _ses


_CLOSING_LINES = {
    "best",
    "regards",
    "warm regards",
    "kind regards",
    "cheers",
    "sincerely",
    "thanks",
    "thank you",
}
_PLACEHOLDER_NAMES = {
    "[your name]",
    "[yourname]",
    "[sender name]",
    "[sender]",
    "[name]",
}


def _strip_sign_off(text: str) -> str:
    """Remove model-generated closings like 'Best,\\n[Your Name]'."""
    lines = text.rstrip().split("\n")
    changed = True
    while changed and lines:
        changed = False
        last = lines[-1].strip()
        last_lower = last.lower()
        if last_lower in _PLACEHOLDER_NAMES:
            lines.pop()
            changed = True
            continue
        if last_lower.rstrip(",") in _CLOSING_LINES:
            lines.pop()
            changed = True
            continue
        if len(lines) >= 2:
            prev = lines[-2].strip().lower().rstrip(",")
            if prev in _CLOSING_LINES and len(last) <= 60:
                lines.pop()
                lines.pop()
                changed = True
    return "\n".join(lines).strip()


def _compose_email(sender_name: str, buddy_name: str, context: str) -> str:
    prompt = (
        f"You are helping {sender_name} share a cooking recipe or message with their "
        f"friend {buddy_name}. Write a warm, concise plain-text email body (no subject "
        f"line, no markdown). Keep it under 200 words. Do not add a sign-off or closing "
        f"(no 'Best', 'Regards', '[Your Name]', or sender name at the end). End after "
        f"the recipe content or your final sentence.\n\n"
        f"Content to share:\n{context}"
    )
    response = _bedrock_client().converse(
        modelId=NOVA_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 512, "temperature": 0.6},
    )
    body = response["output"]["message"]["content"][0]["text"].strip()
    return _strip_sign_off(body)


def _send_email(to_email: str, subject: str, body: str) -> None:
    _ses_client().send_email(
        Source=SES_FROM_EMAIL,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
        },
    )


def handler(event, context):
    if not SES_FROM_EMAIL:
        return {"statusCode": 500, "body": "SES_FROM_EMAIL is not configured"}

    if isinstance(event, str):
        event = json.loads(event)
    if "body" in event and isinstance(event["body"], str):
        try:
            event = json.loads(event["body"])
        except json.JSONDecodeError:
            pass

    sender_name = (event.get("sender_name") or "A Smart Cookbook user").strip()
    subject = (event.get("subject") or "A recipe from Smart Cookbook").strip()
    share_context = (event.get("context") or "").strip()
    buddies = event.get("buddies") or []

    if not share_context:
        return {"statusCode": 400, "body": "Missing context"}
    if not buddies:
        return {"statusCode": 400, "body": "No buddies provided"}

    sent = 0
    errors: list[str] = []

    for buddy in buddies:
        name = (buddy.get("name") or "friend").strip()
        email = (buddy.get("email") or "").strip().lower()
        if not email:
            errors.append(f"Missing email for {name}")
            continue
        try:
            body = _compose_email(sender_name, name, share_context)
            _send_email(email, subject, body)
            sent += 1
        except Exception as exc:
            errors.append(f"{email}: {exc}")

    result = {"sent": sent, "errors": errors}
    status = 200 if sent else 500
    return {"statusCode": status, "body": json.dumps(result)}
