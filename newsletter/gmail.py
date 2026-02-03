import base64
import json
import os
from dataclasses import dataclass
from typing import Optional, Tuple

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except Exception:
    Credentials = None
    InstalledAppFlow = None
    Request = None
    build = None


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class GmailMessage:
    message_id: str
    internal_date: int
    subject: str
    from_email: str
    label_ids: str
    html: Optional[str]
    text: Optional[str]


def ensure_dependencies(beautiful_soup_available: bool) -> None:
    if Credentials is None or InstalledAppFlow is None or build is None:
        raise RuntimeError(
            "Missing Google API dependencies. Install requirements.txt first."
        )
    if not beautiful_soup_available:
        raise RuntimeError("Missing BeautifulSoup. Install requirements.txt first.")


def get_gmail_service(credentials_path: str, token_path: str, *, beautiful_soup_available: bool):
    ensure_dependencies(beautiful_soup_available)
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def resolve_label_id(service, label_name: str) -> Optional[str]:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label.get("name", "").lower() == label_name.lower():
            return label.get("id")
    return None


def list_messages(service, label_id: str, max_results: int, since_query: Optional[str]):
    q = since_query or ""
    response = (
        service.users()
        .messages()
        .list(userId="me", labelIds=[label_id], maxResults=max_results, q=q)
        .execute()
    )
    return response.get("messages", [])


def decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def extract_parts(payload) -> Tuple[Optional[str], Optional[str]]:
    html = None
    text = None

    def walk(part):
        nonlocal html, text
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if data and mime == "text/html":
            html = decode_part(data)
        elif data and mime == "text/plain" and text is None:
            text = decode_part(data)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return html, text


def get_message(service, message_id: str) -> GmailMessage:
    message = (
        service.users().messages().get(userId="me", id=message_id, format="full").execute()
    )
    headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
    subject = headers.get("subject", "")
    from_email = headers.get("from", "")
    label_ids = json.dumps(message.get("labelIds", []))
    internal_date = int(message.get("internalDate", 0))
    html, text = extract_parts(message["payload"])
    return GmailMessage(
        message_id=message_id,
        internal_date=internal_date,
        subject=subject,
        from_email=from_email,
        label_ids=label_ids,
        html=html,
        text=text,
    )
