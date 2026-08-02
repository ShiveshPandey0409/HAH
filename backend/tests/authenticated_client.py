"""Compatibility client that runs legacy milestone fixtures through real HTTP auth.

The production API no longer accepts actor IDs in request bodies. Older business
tests still use those IDs to describe the actor, so this test-only client exchanges
legacy user creation for signup, attaches the issued bearer token, and removes the
now-untrusted actor field before the request reaches FastAPI.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from httpx import AsyncClient, Response

TEST_PASSWORD = "correct horse battery staple"


class AuthenticatedMilestoneClient(AsyncClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._tokens: dict[UUID, str] = {}
        self._task_owners: dict[UUID, UUID] = {}

    def auth_headers(self, user_id: UUID) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens[user_id]}"}

    async def request(
        self,
        method: str,
        url: str,
        *,
        content: Any = None,
        data: Any = None,
        files: Any = None,
        json: Any = None,
        params: Any = None,
        headers: Any = None,
        cookies: Any = None,
        auth: Any = None,
        follow_redirects: bool | None = None,
        timeout: Any = None,
        extensions: Any = None,
    ) -> Response:
        path = self._path(url)
        if method.upper() == "POST" and path == "/v1/users":
            return await self._legacy_create_user(
                url,
                json=json,
                headers=headers,
                follow_redirects=follow_redirects,
                timeout=timeout,
            )

        request_json = dict(json) if isinstance(json, dict) else json
        actor_id = self._actor_from_request(method, path, request_json)
        request_json = self._strip_legacy_actor_fields(method, path, request_json)
        request_headers = dict(headers or {})
        if "authorization" not in {key.lower() for key in request_headers}:
            token = self._tokens.get(actor_id) if actor_id is not None else None
            if token is not None:
                request_headers["Authorization"] = f"Bearer {token}"

        response = await super().request(
            method,
            url,
            content=content,
            data=data,
            files=files,
            json=request_json,
            params=params,
            headers=request_headers,
            cookies=cookies,
            auth=auth,
            follow_redirects=follow_redirects,
            timeout=timeout,
            extensions=extensions,
        )
        if (
            method.upper() == "POST"
            and path == "/v1/tasks"
            and response.status_code == 201
            and actor_id is not None
        ):
            self._task_owners[UUID(response.json()["id"])] = actor_id
        return response

    async def _legacy_create_user(
        self,
        url: str,
        *,
        json: Any,
        headers: Any,
        follow_redirects: bool | None,
        timeout: Any,
    ) -> Response:
        signup_data = dict(json) if isinstance(json, dict) else {}
        signup_data["password"] = TEST_PASSWORD
        response = await super().request(
            "POST",
            "/v1/auth/signup",
            json=signup_data,
            headers=headers,
            follow_redirects=follow_redirects,
            timeout=timeout,
        )
        if response.status_code != 201:
            return response
        body = response.json()
        user_id = UUID(body["user"]["id"])
        self._tokens[user_id] = body["access_token"]
        return Response(
            status_code=response.status_code,
            headers=response.headers,
            json=body["user"],
            request=response.request,
            extensions=response.extensions,
        )

    def _actor_from_request(
        self,
        method: str,
        path: str,
        request_json: Any,
    ) -> UUID | None:
        if isinstance(request_json, dict):
            for field in ("creator_id", "freelancer_id", "verifier_user_id"):
                value = request_json.get(field)
                if value is not None:
                    try:
                        return UUID(str(value))
                    except ValueError:
                        return None

        segments = path.strip("/").split("/")
        if len(segments) >= 3 and segments[1] in {"users", "freelancers"}:
            try:
                candidate = UUID(segments[2])
            except ValueError:
                return None
            if candidate in self._tokens:
                return candidate

        if len(segments) >= 3 and segments[1] == "tasks":
            try:
                task_id = UUID(segments[2])
            except ValueError:
                return None
            return self._task_owners.get(task_id)

        if method.upper() in {"GET", "POST", "PUT", "DELETE"} and self._tokens:
            return next(reversed(self._tokens))
        return None

    @staticmethod
    def _strip_legacy_actor_fields(method: str, path: str, request_json: Any) -> Any:
        if not isinstance(request_json, dict):
            return request_json
        if (method.upper() == "POST" and path == "/v1/tasks") or (
            method.upper() == "PUT" and path.startswith("/v1/tasks/")
        ):
            request_json.pop("creator_id", None)
        elif path.startswith("/v1/bounties/") and path.endswith("/claims"):
            request_json.pop("freelancer_id", None)
        elif path.startswith("/v1/claims/") and path.endswith("/submissions"):
            request_json.pop("freelancer_id", None)
        elif path.startswith("/v1/submissions/") and path.endswith("/verification"):
            request_json.pop("verifier_user_id", None)
            request_json.pop("method", None)
        return request_json

    @staticmethod
    def _path(url: str) -> str:
        return url.split("?", 1)[0]
