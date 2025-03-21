import os
from collections.abc import Iterable
from datetime import datetime
from datetime import timezone
from typing import Any

from jira import JIRA
from jira.resources import Issue

from onyx.configs.app_configs import INDEX_BATCH_SIZE
from onyx.configs.app_configs import JIRA_CONNECTOR_LABELS_TO_SKIP
from onyx.configs.app_configs import JIRA_CONNECTOR_MAX_TICKET_SIZE
from onyx.configs.constants import DocumentSource
from onyx.connectors.cross_connector_utils.miscellaneous_utils import time_str_to_utc
from onyx.connectors.exceptions import ConnectorValidationError
from onyx.connectors.exceptions import CredentialExpiredError
from onyx.connectors.exceptions import InsufficientPermissionsError
from onyx.connectors.interfaces import GenerateDocumentsOutput
from onyx.connectors.interfaces import GenerateSlimDocumentOutput
from onyx.connectors.interfaces import LoadConnector
from onyx.connectors.interfaces import PollConnector
from onyx.connectors.interfaces import SecondsSinceUnixEpoch
from onyx.connectors.interfaces import SlimConnector
from onyx.connectors.models import ConnectorMissingCredentialError
from onyx.connectors.models import Document
from onyx.connectors.models import SlimDocument
from onyx.connectors.models import TextSection
from onyx.connectors.onyx_jira.utils import best_effort_basic_expert_info
from onyx.connectors.onyx_jira.utils import best_effort_get_field_from_issue
from onyx.connectors.onyx_jira.utils import build_jira_client
from onyx.connectors.onyx_jira.utils import build_jira_url
from onyx.connectors.onyx_jira.utils import extract_text_from_adf
from onyx.connectors.onyx_jira.utils import get_comment_strs
from onyx.indexing.indexing_heartbeat import IndexingHeartbeatInterface
from onyx.utils.logger import setup_logger


logger = setup_logger()

JIRA_API_VERSION = os.environ.get("JIRA_API_VERSION") or "2"
_JIRA_SLIM_PAGE_SIZE = 500
_JIRA_FULL_PAGE_SIZE = 50


def _paginate_jql_search(
    jira_client: JIRA,
    jql: str,
    max_results: int,
    fields: str | None = None,
) -> Iterable[Issue]:
    start = 0
    while True:
        logger.debug(
            f"Fetching Jira issues with JQL: {jql}, "
            f"starting at {start}, max results: {max_results}"
        )
        issues = jira_client.search_issues(
            jql_str=jql,
            startAt=start,
            maxResults=max_results,
            fields=fields,
        )

        for issue in issues:
            if isinstance(issue, Issue):
                yield issue
            else:
                raise Exception(f"Found Jira object not of type Issue: {issue}")

        if len(issues) < max_results:
            break

        start += max_results


def fetch_jira_issues_batch(
    jira_client: JIRA,
    jql: str,
    batch_size: int,
    comment_email_blacklist: tuple[str, ...] = (),
    labels_to_skip: set[str] | None = None,
) -> Iterable[Document]:
    for issue in _paginate_jql_search(
        jira_client=jira_client,
        jql=jql,
        max_results=batch_size,
    ):
        if labels_to_skip:
            if any(label in issue.fields.labels for label in labels_to_skip):
                logger.info(
                    f"Skipping {issue.key} because it has a label to skip. Found "
                    f"labels: {issue.fields.labels}. Labels to skip: {labels_to_skip}."
                )
                continue

        description = (
            issue.fields.description
            if JIRA_API_VERSION == "2"
            else extract_text_from_adf(issue.raw["fields"]["description"])
        )
        comments = get_comment_strs(
            issue=issue,
            comment_email_blacklist=comment_email_blacklist,
        )
        ticket_content = f"{description}\n" + "\n".join(
            [f"Comment: {comment}" for comment in comments if comment]
        )

        # Check ticket size
        if len(ticket_content.encode("utf-8")) > JIRA_CONNECTOR_MAX_TICKET_SIZE:
            logger.info(
                f"Skipping {issue.key} because it exceeds the maximum size of "
                f"{JIRA_CONNECTOR_MAX_TICKET_SIZE} bytes."
            )
            continue

        page_url = f"{jira_client.client_info()}/browse/{issue.key}"

        people = set()
        try:
            creator = best_effort_get_field_from_issue(issue, "creator")
            if basic_expert_info := best_effort_basic_expert_info(creator):
                people.add(basic_expert_info)
        except Exception:
            # Author should exist but if not, doesn't matter
            pass

        try:
            assignee = best_effort_get_field_from_issue(issue, "assignee")
            if basic_expert_info := best_effort_basic_expert_info(assignee):
                people.add(basic_expert_info)
        except Exception:
            # Author should exist but if not, doesn't matter
            pass

        metadata_dict = {}
        if priority := best_effort_get_field_from_issue(issue, "priority"):
            metadata_dict["priority"] = priority.name
        if status := best_effort_get_field_from_issue(issue, "status"):
            metadata_dict["status"] = status.name
        if resolution := best_effort_get_field_from_issue(issue, "resolution"):
            metadata_dict["resolution"] = resolution.name
        if labels := best_effort_get_field_from_issue(issue, "labels"):
            metadata_dict["label"] = labels

        yield Document(
            id=page_url,
            sections=[TextSection(link=page_url, text=ticket_content)],
            source=DocumentSource.JIRA,
            semantic_identifier=f"{issue.key}: {issue.fields.summary}",
            title=f"{issue.key} {issue.fields.summary}",
            doc_updated_at=time_str_to_utc(issue.fields.updated),
            primary_owners=list(people) or None,
            # TODO add secondary_owners (commenters) if needed
            metadata=metadata_dict,
        )


class JiraConnector(LoadConnector, PollConnector, SlimConnector):
    def __init__(
        self,
        jira_base_url: str,
        project_key: str | None = None,
        comment_email_blacklist: list[str] | None = None,
        batch_size: int = INDEX_BATCH_SIZE,
        # if a ticket has one of the labels specified in this list, we will just
        # skip it. This is generally used to avoid indexing extra sensitive
        # tickets.
        labels_to_skip: list[str] = JIRA_CONNECTOR_LABELS_TO_SKIP,
    ) -> None:
        self.batch_size = batch_size
        self.jira_base = jira_base_url.rstrip("/")  # Remove trailing slash if present
        self.jira_project = project_key
        self._comment_email_blacklist = comment_email_blacklist or []
        self.labels_to_skip = set(labels_to_skip)

        self._jira_client: JIRA | None = None

    @property
    def comment_email_blacklist(self) -> tuple:
        return tuple(email.strip() for email in self._comment_email_blacklist)

    @property
    def jira_client(self) -> JIRA:
        if self._jira_client is None:
            raise ConnectorMissingCredentialError("Jira")
        return self._jira_client

    @property
    def quoted_jira_project(self) -> str:
        # Quote the project name to handle reserved words
        if not self.jira_project:
            return ""
        return f'"{self.jira_project}"'

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        self._jira_client = build_jira_client(
            credentials=credentials,
            jira_base=self.jira_base,
        )
        return None

    def _get_jql_query(self) -> str:
        """Get the JQL query based on whether a specific project is set"""
        if self.jira_project:
            return f"project = {self.quoted_jira_project}"
        return ""  # Empty string means all accessible projects

    def load_from_state(self) -> GenerateDocumentsOutput:
        jql = self._get_jql_query()

        document_batch = []
        for doc in fetch_jira_issues_batch(
            jira_client=self.jira_client,
            jql=jql,
            batch_size=_JIRA_FULL_PAGE_SIZE,
            comment_email_blacklist=self.comment_email_blacklist,
            labels_to_skip=self.labels_to_skip,
        ):
            document_batch.append(doc)
            if len(document_batch) >= self.batch_size:
                yield document_batch
                document_batch = []

        yield document_batch

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        start_date_str = datetime.fromtimestamp(start, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
        end_date_str = datetime.fromtimestamp(end, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )

        base_jql = self._get_jql_query()
        jql = (
            f"{base_jql} AND " if base_jql else ""
        ) + f"updated >= '{start_date_str}' AND updated <= '{end_date_str}'"

        document_batch = []
        for doc in fetch_jira_issues_batch(
            jira_client=self.jira_client,
            jql=jql,
            batch_size=_JIRA_FULL_PAGE_SIZE,
            comment_email_blacklist=self.comment_email_blacklist,
            labels_to_skip=self.labels_to_skip,
        ):
            document_batch.append(doc)
            if len(document_batch) >= self.batch_size:
                yield document_batch
                document_batch = []

        yield document_batch

    def retrieve_all_slim_documents(
        self,
        start: SecondsSinceUnixEpoch | None = None,
        end: SecondsSinceUnixEpoch | None = None,
        callback: IndexingHeartbeatInterface | None = None,
    ) -> GenerateSlimDocumentOutput:
        jql = self._get_jql_query()

        slim_doc_batch = []
        for issue in _paginate_jql_search(
            jira_client=self.jira_client,
            jql=jql,
            max_results=_JIRA_SLIM_PAGE_SIZE,
            fields="key",
        ):
            issue_key = best_effort_get_field_from_issue(issue, "key")
            id = build_jira_url(self.jira_client, issue_key)
            slim_doc_batch.append(
                SlimDocument(
                    id=id,
                    perm_sync_data=None,
                )
            )
            if len(slim_doc_batch) >= _JIRA_SLIM_PAGE_SIZE:
                yield slim_doc_batch
                slim_doc_batch = []

        yield slim_doc_batch

    def validate_connector_settings(self) -> None:
        if self._jira_client is None:
            raise ConnectorMissingCredentialError("Jira")

        # If a specific project is set, validate it exists
        if self.jira_project:
            try:
                self.jira_client.project(self.jira_project)
            except Exception as e:
                status_code = getattr(e, "status_code", None)

                if status_code == 401:
                    raise CredentialExpiredError(
                        "Jira credential appears to be expired or invalid (HTTP 401)."
                    )
                elif status_code == 403:
                    raise InsufficientPermissionsError(
                        "Your Jira token does not have sufficient permissions for this project (HTTP 403)."
                    )
                elif status_code == 404:
                    raise ConnectorValidationError(
                        f"Jira project not found with key: {self.jira_project}"
                    )
                elif status_code == 429:
                    raise ConnectorValidationError(
                        "Validation failed due to Jira rate-limits being exceeded. Please try again later."
                    )

                raise RuntimeError(f"Unexpected Jira error during validation: {e}")
        else:
            # If no project specified, validate we can access the Jira API
            try:
                # Try to list projects to validate access
                self.jira_client.projects()
            except Exception as e:
                status_code = getattr(e, "status_code", None)
                if status_code == 401:
                    raise CredentialExpiredError(
                        "Jira credential appears to be expired or invalid (HTTP 401)."
                    )
                elif status_code == 403:
                    raise InsufficientPermissionsError(
                        "Your Jira token does not have sufficient permissions to list projects (HTTP 403)."
                    )
                elif status_code == 429:
                    raise ConnectorValidationError(
                        "Validation failed due to Jira rate-limits being exceeded. Please try again later."
                    )

                raise RuntimeError(f"Unexpected Jira error during validation: {e}")


if __name__ == "__main__":
    import os

    connector = JiraConnector(
        jira_base_url=os.environ["JIRA_BASE_URL"],
        project_key=os.environ.get("JIRA_PROJECT_KEY"),
        comment_email_blacklist=[],
    )

    connector.load_credentials(
        {
            "jira_user_email": os.environ["JIRA_USER_EMAIL"],
            "jira_api_token": os.environ["JIRA_API_TOKEN"],
        }
    )
    document_batches = connector.load_from_state()
    print(next(document_batches))
