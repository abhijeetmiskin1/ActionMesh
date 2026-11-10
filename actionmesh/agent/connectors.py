"""Bounded official API reads. Document bodies, issue text and secrets stay out of results."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any
from urllib.parse import urlencode

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ProviderError
from actionmesh.providers.real_base import IntegrationConfig

GITHUB_REPOS = "github.repositories.search"
GITHUB_ISSUES = "github.issues.list"
DRIVE_SEARCH = "google.drive.search"
CONNECTOR_ACTIONS = frozenset({GITHUB_REPOS, GITHUB_ISSUES, DRIVE_SEARCH})
GITHUB_TOKEN = "ACTIONMESH_GITHUB_TOKEN"
DRIVE_TOKEN = "ACTIONMESH_GOOGLE_DRIVE_TOKEN"
PAGE_SIZE = 10
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")


async def read(action: Action) -> dict[str, Any]:
    from actionmesh.voiceos.transport import ReadTransportError, TransportError, get_json

    drive = action.type == DRIVE_SEARCH
    raw = action.parameters.get("repository" if action.type == GITHUB_ISSUES else "query")
    if not isinstance(raw, str) or not 1 <= len(raw.strip()) <= 120 or not raw.isprintable():
        raise ProviderError("Supply a search term of up to 120 characters.", FailureKind.INVALID_INPUT)
    term = raw.strip()
    if action.type == GITHUB_ISSUES and not _REPO.fullmatch(term):
        raise ProviderError("Specify the GitHub repository as owner/name.", FailureKind.INVALID_INPUT)
    if action.type != GITHUB_ISSUES and not re.fullmatch(r"[\w .+&'()/-]+", term):
        raise ProviderError("Use a plain search term without advanced filters.", FailureKind.INVALID_INPUT)

    if drive:
        token = IntegrationConfig("Google Drive", "https://www.googleapis.com", DRIVE_TOKEN).credential()
        endpoint = "https://www.googleapis.com/drive/v3/files"
        escaped = term.replace("'", "\\'")
        query = {
            "q": f"trashed = false and name contains '{escaped}'",
            "fields": "nextPageToken,incompleteSearch,files(id)",
            "pageSize": str(PAGE_SIZE),
        }
        source = "https://drive.google.com/drive/search?" + urlencode({"q": term})
    else:
        token = os.getenv(GITHUB_TOKEN, "")
        issues = action.type == GITHUB_ISSUES
        endpoint = "https://api.github.com/search/" + ("issues" if issues else "repositories")
        # The repository qualifier is built from a validated owner/name. Public
        # repository discovery explicitly excludes private repository metadata.
        search = f"repo:{term} is:issue is:open" if issues else f"{term} is:public in:name"
        query = {"q": search, "per_page": str(PAGE_SIZE)}
        source = (
            f"https://github.com/{term}/issues"
            if issues else "https://github.com/search?" + urlencode({"q": term, "type": "repositories"})
        )
    headers = {"authorization": f"Bearer {token}"} if token else {}
    try:
        response = await get_json(endpoint=endpoint, headers=headers, query=query)
    except ReadTransportError as exc:
        if exc.status == 429 or exc.rate_limited:
            detail = "The service hit a rate limit. Try again later."
        elif exc.status == 403:
            detail = "The service refused access. Check the token permissions and repository access."
        elif exc.status == 401:
            detail = "The service rejected the credential. Renew the read-only authorization."
        elif exc.status == 404:
            detail = "The resource was not found or is outside this credential's access."
        else:
            detail = f"The service could not complete the read (HTTP {exc.status})."
        raise ProviderError(detail, FailureKind.PROVIDER) from exc
    except TransportError as exc:
        raise ProviderError("The read service was unavailable or returned invalid data.", FailureKind.PROVIDER) from exc

    items = response.get("files" if drive else "items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ProviderError("The service returned an invalid result list.", FailureKind.PROVIDER)
    ids = [item.get("id") for item in items[:PAGE_SIZE]]
    if any(type(item_id) not in {int, str} or not str(item_id) for item_id in ids):
        raise ProviderError("The service did not return stable result IDs.", FailureKind.PROVIDER)
    # Compare independently read page membership without retaining any resource ID.
    membership = hashlib.sha256(repr(sorted(str(item_id) for item_id in ids)).encode()).hexdigest()
    result: dict[str, Any] = {
        "count": min(len(items), PAGE_SIZE),
        "truncated": len(items) > PAGE_SIZE or bool(
            response.get("nextPageToken") or response.get("incompleteSearch")
            or response.get("incomplete_results")
        ),
        "source_url": source,
        "_membership": membership,
    }
    total = response.get("total_count")
    if not drive and type(total) is int and total >= len(items):
        result["total"] = total
        result["truncated"] |= total > PAGE_SIZE
    if action.type == GITHUB_REPOS:
        result["connector_results"] = [
            {"name": name, "source_url": f"https://github.com/{name}"}
            for item in items[:PAGE_SIZE]
            if item.get("private") is False
            and isinstance(name := item.get("full_name"), str) and _REPO.fullmatch(name)
        ]
    elif action.type == GITHUB_ISSUES:
        result["connector_results"] = [
            {"name": f"Issue #{number}", "source_url": f"https://github.com/{term}/issues/{number}"}
            for item in items[:PAGE_SIZE]
            if type(number := item.get("number")) is int and number > 0
        ]
    if action.type == GITHUB_REPOS and total == 1 and len(items) == 1 and not result["truncated"]:
        repository = items[0].get("full_name")
        if isinstance(repository, str) and _REPO.fullmatch(repository) and items[0].get("private") is False:
            result["selected_repository"] = repository
            result["unique"] = True
    return result
