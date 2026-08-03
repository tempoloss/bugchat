"""Клиент self-hosted Plane на сессионной авторизации.

API-токены в этой сборке выключены (`/api-tokens/` → 404), поэтому работаем
как браузер: CSRF → sign-in → cookie `session-id`, а на запись добавляем
заголовок `X-CSRFTOKEN`.

Особенности сборки, проверенные на живом инстансе:
  * метки цепляются ключом ``label_ids``; ``labels`` молча игнорируется;
  * PATCH задачи отвечает 204 с пустым телом — парсить JSON нельзя;
  * вложения грузятся через assets v2: init → presigned POST → PATCH.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import httpx

from bugbot.config import Config

logger = logging.getLogger(__name__)

_LABEL_COLORS = {"telegram": "#2AABEE", "bug": "#E11D48"}
_DEFAULT_LABEL_COLOR = "#6B7280"


class PlaneError(RuntimeError):
    pass


def image_component(asset_id: str, *, width: int, height: int) -> str:
    """Разметка встроенной картинки для редактора Plane.

    Проверено на живом инстансе: обычный `<img src=...>` редактор показывает как
    «Error loading image», отрисовывается только собственный узел image-component.
    Настоящее соотношение сторон отдаём из Telegram, иначе редактор резервирует
    место не по размеру и под картинкой висит пустота.
    """
    aspect = round(width / height, 4) if width and height else 1.0
    return f'<image-component src="{asset_id}" width="60%" height="auto" aspectratio="{aspect}"></image-component>'


@dataclass(frozen=True, slots=True)
class CreatedIssue:
    id: str
    sequence_id: int
    key: str
    url: str
    name: str


@dataclass(frozen=True, slots=True)
class IssueState:
    state_id: str
    group: str
    updated_by: str | None
    name: str


class PlaneClient:
    def __init__(self, config: Config) -> None:
        self._cfg = config
        self._base = config.plane_base
        self._ws = config.plane_workspace
        self._client = httpx.AsyncClient(
            verify=config.plane_verify_tls,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._project_id: str = ""
        self._identifier: str = ""
        self._states: dict[str, str] = {}
        self._state_groups: dict[str, str] = {}
        """state_id → группа (backlog/unstarted/started/completed/cancelled)."""
        self._state_names: dict[str, str] = {}
        self._members: dict[str, str] = {}
        self._label_ids: list[str] = []

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- авторизация ----------------------------------------------------
    async def login(self) -> None:
        csrf = await self._client.get(f"{self._base}/auth/get-csrf-token/")
        csrf.raise_for_status()
        token = csrf.json()["csrf_token"]

        response = await self._client.post(
            f"{self._base}/auth/sign-in/",
            data={"csrfmiddlewaretoken": token, "email": self._cfg.plane_email, "password": self._cfg.plane_password},
            # Referer/Origin обязательны, иначе Django заворачивает запрос как CSRF failure.
            headers={"Referer": f"{self._base}/", "Origin": self._base},
        )
        if response.status_code != 302:
            raise PlaneError(f"вход в Plane не удался: {response.status_code} {response.text[:200]}")
        logger.info("plane: вошли как %s", self._cfg.plane_email)

    def _headers(self, *, json_body: bool = True) -> dict[str, str]:
        headers = {
            "Referer": f"{self._base}/",
            "Origin": self._base,
            "X-CSRFTOKEN": self._client.cookies.get("csrftoken") or "",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        _retry_auth: bool = True,
    ) -> httpx.Response:
        response = await self._client.request(method, url, json=json, headers=self._headers(json_body=json is not None))
        if response.status_code in (401, 403) and _retry_auth:
            logger.info("plane: сессия протухла, логинимся заново")
            await self.login()
            return await self._request(method, url, json=json, _retry_auth=False)
        if response.status_code >= 400:
            raise PlaneError(f"{method} {url.replace(self._base, '')}: {response.status_code} {response.text[:300]}")
        return response

    # ---- разрешение справочников ---------------------------------------
    @property
    def _project_url(self) -> str:
        return f"{self._base}/api/workspaces/{self._ws}/projects/{self._project_id}"

    async def bootstrap(self) -> None:
        """Разрешает проект, состояния и метки один раз на старте."""
        await self.login()

        wanted = self._cfg.plane_project.strip().lower()
        projects = (await self._request("GET", f"{self._base}/api/workspaces/{self._ws}/projects/")).json()
        for project in projects:
            if wanted in (project["identifier"].lower(), project["name"].lower()):
                self._project_id = project["id"]
                self._identifier = project["identifier"]
                break
        else:
            available = ", ".join(p["identifier"] for p in projects)
            raise PlaneError(f"проект {self._cfg.plane_project!r} не найден в {self._ws}; есть: {available}")

        states = (await self._request("GET", f"{self._project_url}/states/")).json()
        self._states = {state["name"].lower(): state["id"] for state in states}
        self._state_groups = {state["id"]: state["group"] for state in states}
        self._state_names = {state["id"]: state["name"] for state in states}
        for required in (self._cfg.plane_state, self._cfg.plane_cancel_state):
            if required.lower() not in self._states:
                raise PlaneError(f"состояние {required!r} не найдено в проекте {self._identifier}")

        self._members = await self._load_members()

        self._label_ids = await self._ensure_labels(self._cfg.plane_labels)
        logger.info(
            "plane: проект %s (%s), состояние %s, меток %d",
            self._identifier,
            self._project_id,
            self._cfg.plane_state,
            len(self._label_ids),
        )

    async def _ensure_labels(self, names: tuple[str, ...]) -> list[str]:
        if not names:
            return []
        existing = (await self._request("GET", f"{self._project_url}/issue-labels/")).json()
        by_name = {label["name"].lower(): label["id"] for label in existing}

        ids: list[str] = []
        for name in names:
            found = by_name.get(name.lower())
            if found is None:
                color = _LABEL_COLORS.get(name.lower(), _DEFAULT_LABEL_COLOR)
                created = await self._request(
                    "POST", f"{self._project_url}/issue-labels/", json={"name": name, "color": color}
                )
                found = created.json()["id"]
                logger.info("plane: создана метка %s", name)
            ids.append(found)
        return ids

    async def _load_members(self) -> dict[str, str]:
        """user_id → отображаемое имя: нужно, чтобы написать в чат, кто закрыл задачу."""
        try:
            people = (await self._request("GET", f"{self._base}/api/workspaces/{self._ws}/members/")).json()
        except PlaneError as exc:
            logger.warning("plane: список участников не получен (%s), имена будут без расшифровки", exc)
            return {}
        names: dict[str, str] = {}
        for row in people:
            person = row.get("member") or {}
            if person.get("id"):
                names[person["id"]] = person.get("display_name") or person.get("email") or "кто-то"
        return names

    def member_name(self, user_id: str | None) -> str:
        return self._members.get(user_id or "", "кто-то")

    def state_id(self, name: str) -> str:
        state = self._states.get(name.lower())
        if state is None:
            raise PlaneError(f"состояние {name!r} не найдено")
        return state

    def state_name(self, state_id: str) -> str:
        return self._state_names.get(state_id, "?")

    def state_group(self, state_id: str) -> str:
        return self._state_groups.get(state_id, "")

    @property
    def new_issue_group(self) -> str:
        return self.state_group(self.state_id(self._cfg.plane_state))

    def issue_url(self, sequence_id: int) -> str:
        """Короткая ссылка вида /<workspace>/browse/<KEY>-5/ — именно на неё Plane и редиректит."""
        return f"{self._base}/{self._ws}/browse/{self._identifier}-{sequence_id}/"

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def identifier(self) -> str:
        """Префикс задач проекта, например BL."""
        return self._identifier

    # ---- задачи ---------------------------------------------------------
    async def create_issue(self, *, name: str, description_html: str, priority: str) -> CreatedIssue:
        payload = {
            "name": name,
            "description_html": description_html,
            "priority": priority,
            "state": self.state_id(self._cfg.plane_state),
            "label_ids": self._label_ids,
        }
        issue = (await self._request("POST", f"{self._project_url}/issues/", json=payload)).json()
        issue_id = issue["id"]

        # Сериализатор create в части сборок глотает label_ids — досылаем PATCH-ем,
        # иначе метка `telegram` отвалилась бы молча и фильтр на доске стал бы пустым.
        if self._label_ids and not issue.get("label_ids"):
            await self._request("PATCH", f"{self._project_url}/issues/{issue_id}/", json={"label_ids": self._label_ids})

        sequence_id = issue["sequence_id"]
        return CreatedIssue(
            id=issue_id,
            sequence_id=sequence_id,
            key=f"{self._identifier}-{sequence_id}",
            url=self.issue_url(sequence_id),
            name=issue.get("name") or name,
        )

    async def update_description(self, issue_id: str, description_html: str) -> None:
        await self._request(
            "PATCH", f"{self._project_url}/issues/{issue_id}/", json={"description_html": description_html}
        )

    async def set_state(self, issue_id: str, state_name: str) -> None:
        await self._request(
            "PATCH", f"{self._project_url}/issues/{issue_id}/", json={"state": self.state_id(state_name)}
        )

    async def add_comment(self, issue_id: str, comment_html: str) -> str:
        created = await self._request(
            "POST", f"{self._project_url}/issues/{issue_id}/comments/", json={"comment_html": comment_html}
        )
        return created.json()["id"]

    async def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        payload = (await self._request("GET", f"{self._project_url}/issues/{issue_id}/comments/")).json()
        return payload["results"] if isinstance(payload, dict) else payload

    async def upload_description_image(self, issue_id: str, *, filename: str, mime: str, data: bytes) -> str | None:
        """Кладёт картинку как ассет описания и возвращает asset_id для `<image-component>`.

        Отдельный от вложений эндпоинт: у ассетов описания свой `entity_type`,
        и только они отдаются редактору как встроенные изображения.
        """
        base = f"{self._base}/api/assets/v2/workspaces/{self._ws}/projects/{self._project_id}/"
        try:
            init = (
                await self._request(
                    "POST",
                    base,
                    json={
                        "name": filename,
                        "type": mime,
                        "size": len(data),
                        "entity_type": "ISSUE_DESCRIPTION",
                        "entity_identifier": issue_id,
                    },
                )
            ).json()
            upload = init["upload_data"]
            stored = await self._client.post(
                upload["url"], data=upload["fields"], files={"file": (filename, data, mime)}
            )
            if stored.status_code not in (200, 201, 204):
                raise PlaneError(f"хранилище отвергло картинку: {stored.status_code} {stored.text[:200]}")
            await self._request("PATCH", f"{base}{init['asset_id']}/", json={})
        except (PlaneError, httpx.HTTPError, KeyError) as exc:
            logger.warning("картинка %s не встроилась: %s", filename, exc)
            return None
        return init["asset_id"]

    async def issue_states(self) -> dict[str, IssueState]:
        """Состояние всех задач проекта одним запросом — список отдаёт `state__group`,
        так что для детекта закрытия детальный GET не нужен."""
        payload = (await self._request("GET", f"{self._project_url}/issues/")).json()
        rows = payload["results"] if isinstance(payload, dict) else payload
        return {
            row["id"]: IssueState(
                state_id=row["state_id"],
                group=row.get("state__group") or self.state_group(row["state_id"]),
                updated_by=row.get("updated_by"),
                name=row.get("name") or "",
            )
            for row in rows
        }

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        return (await self._request("GET", f"{self._project_url}/issues/{issue_id}/")).json()

    # ---- вложения (assets v2) -------------------------------------------
    async def upload_attachment(self, issue_id: str, *, filename: str, mime: str, data: bytes) -> bool:
        base = (
            f"{self._base}/api/assets/v2/workspaces/{self._ws}"
            f"/projects/{self._project_id}/issues/{issue_id}/attachments/"
        )
        try:
            init = (await self._request("POST", base, json={"name": filename, "type": mime, "size": len(data)})).json()
            upload = init["upload_data"]

            # presigned POST в MinIO: свои заголовки (никакого JSON content-type и CSRF).
            stored = await self._client.post(
                upload["url"], data=upload["fields"], files={"file": (filename, data, mime)}
            )
            if stored.status_code not in (200, 201, 204):
                raise PlaneError(f"хранилище отвергло файл: {stored.status_code} {stored.text[:200]}")

            await self._request("PATCH", f"{base}{init['asset_id']}/", json={})
        except (PlaneError, httpx.HTTPError, KeyError) as exc:
            logger.warning("вложение %s не загрузилось: %s", filename, exc)
            return False
        return True
