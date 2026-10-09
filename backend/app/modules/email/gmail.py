"""Mailbox provider interface, the Gmail implementation, and a local stand-in.

``GmailProvider`` is contract-tested against a mocked HTTP transport. It has not been
verified against Google's live API in this build. ``FakeMailbox`` reproduces the parts of
Gmail's behaviour that synchronisation depends on: history IDs, paging, an expired
history cursor, and notifications that can be dropped.
"""

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)
REQUIRED_SCOPES = frozenset(SCOPES[2:])


class MailboxError(Exception):
    """A provider call failed. ``ambiguous`` means the request may have taken effect."""

    def __init__(
        self,
        message: str,
        *,
        ambiguous: bool = False,
        rate_limited: bool = False,
        revoked: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.ambiguous, self.rate_limited, self.revoked, self.retry_after = (
            ambiguous,
            rate_limited,
            revoked,
            retry_after,
        )


class HistoryExpired(Exception):
    """The stored history cursor is too old or invalid; a full synchronisation is needed."""


@dataclass
class RawMessage:
    id: str
    thread_id: str
    label_ids: list[str]
    internal_date: datetime
    headers: dict[str, str]
    text: str = ""
    html: str = ""
    snippet: str = ""
    content_type: str = "text/plain"
    attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class HistoryPage:
    message_ids: list[str]
    max_record_id: str | None
    mailbox_history_id: str
    next_page_token: str | None


class MailboxProvider(Protocol):
    def profile(self, token: str) -> tuple[str, str]: ...
    def watch(self, token: str, topic: str) -> tuple[str, datetime]: ...
    def list_history(self, token: str, start_history_id: str, page_token: str | None) -> HistoryPage: ...
    def list_messages(self, token: str, query: str, page_token: str | None) -> tuple[list[str], str | None]: ...
    def get_message(self, token: str, message_id: str) -> RawMessage | None: ...
    def send(self, token: str, raw: bytes, thread_id: str | None) -> tuple[str, str]: ...
    def stop(self, token: str) -> None: ...
    def find_by_rfc_id(self, token: str, rfc_message_id: str) -> tuple[str, str] | None: ...


def _decode(data: str | None) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _walk(payload: dict[str, Any], out: RawMessage) -> None:
    mime = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    filename = payload.get("filename")
    if filename:
        # Attachments are listed, never downloaded.
        out.attachments.append({"filename": filename[:255], "mime_type": mime, "size": int(body.get("size") or 0)})
    elif mime == "text/plain" and not out.text:
        out.text = _decode(body.get("data"))
    elif mime == "text/html" and not out.html:
        out.html = _decode(body.get("data"))
    for part in payload.get("parts") or []:
        _walk(part, out)


class GmailProvider:
    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(timeout=20)

    def _call(
        self, method: str, url: str, token: str, *, ambiguous_on_failure: bool = False, **kwargs: Any
    ) -> httpx.Response:
        try:
            response = self._http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        except httpx.TimeoutException as exc:
            raise MailboxError("The mailbox provider timed out.", ambiguous=ambiguous_on_failure) from exc
        except httpx.HTTPError as exc:
            raise MailboxError("The mailbox provider could not be reached.", ambiguous=ambiguous_on_failure) from exc
        if response.status_code == 401:
            raise MailboxError("The mailbox connection is no longer authorised.", revoked=True)
        if response.status_code == 429 or (
            response.status_code == 403 and "ratelimit" in response.text.lower().replace(" ", "")
        ):
            retry = response.headers.get("retry-after")
            raise MailboxError(
                "The mailbox provider is rate limiting requests.",
                rate_limited=True,
                retry_after=int(retry) if retry and retry.isdigit() else None,
            )
        if response.status_code >= 500:
            raise MailboxError("The mailbox provider returned a server error.", ambiguous=ambiguous_on_failure)
        return response

    def profile(self, token: str) -> tuple[str, str]:
        data = self._ok(self._call("GET", f"{GMAIL}/profile", token))
        return data["emailAddress"].lower(), str(data["historyId"])

    def watch(self, token: str, topic: str) -> tuple[str, datetime]:
        data = self._ok(
            self._call(
                "POST",
                f"{GMAIL}/watch",
                token,
                json={"topicName": topic, "labelIds": ["INBOX", "SENT"], "labelFilterBehavior": "INCLUDE"},
            )
        )
        return str(data["historyId"]), datetime.fromtimestamp(int(data["expiration"]) / 1000, tz=UTC)

    def list_history(self, token: str, start_history_id: str, page_token: str | None) -> HistoryPage:
        params = {"startHistoryId": start_history_id, "historyTypes": "messageAdded", "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        response = self._call("GET", f"{GMAIL}/history", token, params=params)
        if response.status_code == 404:
            raise HistoryExpired
        data = self._ok(response)
        records = data.get("history") or []
        ids = [added["message"]["id"] for record in records for added in record.get("messagesAdded") or []]
        return HistoryPage(
            message_ids=list(dict.fromkeys(ids)),
            max_record_id=max((str(r["id"]) for r in records), key=int, default=None),
            mailbox_history_id=str(data["historyId"]),
            next_page_token=data.get("nextPageToken"),
        )

    def list_messages(self, token: str, query: str, page_token: str | None) -> tuple[list[str], str | None]:
        params = {"q": query, "maxResults": 200}
        if page_token:
            params["pageToken"] = page_token
        data = self._ok(self._call("GET", f"{GMAIL}/messages", token, params=params))
        return [m["id"] for m in data.get("messages") or []], data.get("nextPageToken")

    def get_message(self, token: str, message_id: str) -> RawMessage | None:
        response = self._call("GET", f"{GMAIL}/messages/{message_id}", token, params={"format": "full"})
        if response.status_code == 404:
            return None  # deleted between the listing and the fetch
        data = self._ok(response)
        payload = data.get("payload") or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or []}
        message = RawMessage(
            id=data["id"],
            thread_id=data["threadId"],
            label_ids=list(data.get("labelIds") or []),
            internal_date=datetime.fromtimestamp(int(data.get("internalDate") or 0) / 1000, tz=UTC),
            headers=headers,
            snippet=data.get("snippet") or "",
            content_type=headers.get("content-type", payload.get("mimeType") or ""),
        )
        _walk(payload, message)
        return message

    def send(self, token: str, raw: bytes, thread_id: str | None) -> tuple[str, str]:
        body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}
        if thread_id:
            body["threadId"] = thread_id
        # A timeout or server error here is ambiguous: Gmail may have accepted the message.
        response = self._call("POST", f"{GMAIL}/messages/send", token, ambiguous_on_failure=True, json=body)
        if response.status_code != 200:
            raise MailboxError(f"The mailbox provider refused the message ({response.status_code}).")
        data = response.json()
        return data["id"], data["threadId"]

    def stop(self, token: str) -> None:
        """Stop push notifications for the mailbox."""
        self._ok(self._call("POST", f"{GMAIL}/stop", token))

    def find_by_rfc_id(self, token: str, rfc_message_id: str) -> tuple[str, str] | None:
        data = self._ok(
            self._call(
                "GET",
                f"{GMAIL}/messages",
                token,
                params={"q": f"rfc822msgid:{rfc_message_id.strip('<>')}", "maxResults": 1},
            )
        )
        found = data.get("messages") or []
        return (found[0]["id"], found[0]["threadId"]) if found else None

    @staticmethod
    def _ok(response: httpx.Response) -> dict[str, Any]:
        if response.status_code != 200:
            raise MailboxError(f"The mailbox provider refused the request ({response.status_code}).")
        data: dict[str, Any] = response.json()
        return data


class FakeMailbox:
    """An in-memory mailbox with Gmail-like history. One instance stands for one account."""

    def __init__(self, address: str = "sales@seweb.example", page_size: int = 2) -> None:
        self.address, self.page_size = address, page_size
        self.messages: dict[str, RawMessage] = {}
        self.history: list[tuple[int, str]] = []
        self.history_id = 1000
        self.oldest_history = 0  # cursors below this are "expired"
        self.sent: list[bytes] = []
        self.calls: list[str] = []
        self.fail: dict[str, list[Exception]] = {}
        self.watch_expiry = datetime(2026, 10, 15, tzinfo=UTC)
        self.send_lands_despite_error = False

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        queue = self.fail.get(name)
        if queue:
            raise queue.pop(0)

    def add(
        self,
        *,
        sender: str,
        to: str | None = None,
        subject: str = "Hello",
        text: str = "Hi",
        thread_id: str | None = None,
        labels: tuple[str, ...] = ("INBOX",),
        headers: dict[str, str] | None = None,
        html: str = "",
        message_id: str | None = None,
        content_type: str = "text/plain",
        attachments: list[dict[str, Any]] | None = None,
    ) -> RawMessage:
        self.history_id += 1
        mid = f"m{self.history_id}"
        all_headers = {
            "from": sender,
            "to": to or self.address,
            "subject": subject,
            "message-id": message_id or f"<{mid}@mail.example>",
            "date": "Thu, 08 Oct 2026 10:00:00 +0000",
            **{k.lower(): v for k, v in (headers or {}).items()},
        }
        message = RawMessage(
            id=mid,
            thread_id=thread_id or f"t{self.history_id}",
            label_ids=list(labels),
            internal_date=datetime.now(UTC),
            headers=all_headers,
            text=text,
            html=html,
            snippet=text[:80],
            content_type=content_type,
            attachments=attachments or [],
        )
        self.messages[mid] = message
        self.history.append((self.history_id, mid))
        return message

    def profile(self, token: str) -> tuple[str, str]:
        self._maybe_fail("profile")
        return self.address, str(self.history_id)

    def watch(self, token: str, topic: str) -> tuple[str, datetime]:
        self._maybe_fail("watch")
        return str(self.history_id), self.watch_expiry

    def list_history(self, token: str, start_history_id: str, page_token: str | None) -> HistoryPage:
        self._maybe_fail("list_history")
        start = int(start_history_id)
        if start < self.oldest_history:
            raise HistoryExpired
        records = [(h, m) for h, m in self.history if h > start]
        offset = int(page_token or 0)
        chunk = records[offset : offset + self.page_size]
        more = offset + self.page_size < len(records)
        return HistoryPage(
            message_ids=[m for _, m in chunk],
            max_record_id=str(chunk[-1][0]) if chunk else None,
            mailbox_history_id=str(self.history_id),
            next_page_token=str(offset + self.page_size) if more else None,
        )

    def list_messages(self, token: str, query: str, page_token: str | None) -> tuple[list[str], str | None]:
        self._maybe_fail("list_messages")
        ids = list(self.messages)
        offset = int(page_token or 0)
        more = offset + self.page_size < len(ids)
        return ids[offset : offset + self.page_size], str(offset + self.page_size) if more else None

    def get_message(self, token: str, message_id: str) -> RawMessage | None:
        self._maybe_fail("get_message")
        return self.messages.get(message_id)

    def send(self, token: str, raw: bytes, thread_id: str | None) -> tuple[str, str]:
        from email import message_from_bytes

        self.calls.append("send")
        queue = self.fail.get("send")
        error = queue.pop(0) if queue else None
        if error is None or self.send_lands_despite_error:
            parsed = message_from_bytes(raw)
            stored = self.add(
                sender=parsed["From"],
                to=parsed["To"],
                subject=parsed["Subject"],
                labels=("SENT",),
                thread_id=thread_id,
                text=parsed.get_payload(decode=True).decode("utf-8", errors="replace"),  # type: ignore[union-attr]
                message_id=parsed["Message-ID"],
                headers={
                    k: v
                    for k, v in parsed.items()
                    if k.lower() in ("in-reply-to", "references", "list-unsubscribe", "list-unsubscribe-post")
                },
            )
            self.sent.append(raw)
            if error is None:
                return stored.id, stored.thread_id
        raise error

    def stop(self, token: str) -> None:
        self._maybe_fail("stop")
        self.stopped = True

    def find_by_rfc_id(self, token: str, rfc_message_id: str) -> tuple[str, str] | None:
        self._maybe_fail("find_by_rfc_id")
        for message in self.messages.values():
            if message.headers.get("message-id") == rfc_message_id:
                return message.id, message.thread_id
        return None
