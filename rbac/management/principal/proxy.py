#
# Copyright 2019 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""Proxy for principal management."""
import logging
import os

import requests
from django.conf import settings
from management.models import Principal
from prometheus_client import Counter, Histogram
from rest_framework import status

from rbac.env import ENVIRONMENT

LOGGER = logging.getLogger(__name__)
PROTOCOL = "protocol"
HOST = "host"
PORT = "port"
PATH = "path"
SSL_VERIFY = "ssl_verify"
SOURCE_CERT = "source_cert"
USER_ENV = "env"
CLIENT_ID = "clientid"
API_TOKEN = "apitoken"
USER_ENV_HEADER = "x-rh-insights-env"
CLIENT_ID_HEADER = "x-rh-clientid"
API_TOKEN_HEADER = "x-rh-apitoken"

bop_request_time_tracking = Histogram(
    "rbac_proxy_request_processing_seconds", "Time spent processing requests to BOP from RBAC"
)
bop_request_status_count = Counter(
    "bop_request_status_total", "Number of requests from RBAC to BOP and resulting status", ["method", "status"]
)


class PrincipalProxy:  # pylint: disable=too-few-public-methods
    """A class to handle interactions with the Principal proxy service."""

    def __init__(self):
        """Establish proxy connection information."""
        proxy_conn_info = self._get_proxy_service()
        self.protocol = proxy_conn_info.get(PROTOCOL)
        self.host = proxy_conn_info.get(HOST)
        self.port = proxy_conn_info.get(PORT)
        self.path = proxy_conn_info.get(PATH)
        self.ssl_verify = proxy_conn_info.get(SSL_VERIFY)
        self.source_cert = proxy_conn_info.get(SOURCE_CERT)
        self.user_env = proxy_conn_info.get(USER_ENV)
        self.client_id = proxy_conn_info.get(CLIENT_ID)
        self.api_token = proxy_conn_info.get(API_TOKEN)
        self.client_cert = os.path.join(settings.BASE_DIR, "management", "principal", "certs", "client.pem")

    @staticmethod
    def _create_params(limit=None, offset=None, options={}):
        """Create query parameters."""
        params = {}
        if limit:
            params["limit"] = limit
        if offset:
            params["offset"] = offset
        if "sort_order" in options:
            # BOP only accepts 'des'
            if options["sort_order"] == "desc":
                params["sortOrder"] = "des"
            else:
                params["sortOrder"] = options["sort_order"]
        if "status" in options:
            params["status"] = options["status"]
        if "admin_only" in options:
            params["admin_only"] = options["admin_only"]
        if "query_by" in options:
            if options["query_by"] == "user_id":
                params["queryBy"] = "userId"
            else:
                params["queryBy"] = options["query_by"]

        return params

    def _process_data(self, data, account, account_filter, return_id=False):
        """Process data for uniform output."""
        processed_data = []
        for item in data:
            if account_filter:
                if account == item.get("account_number"):
                    processed_data.append(self._call_item(item, return_id))
            else:
                processed_data.append(self._call_item(item, return_id))

        return processed_data

    @staticmethod
    def _call_item(item, return_id=False):
        processed_item = {
            "username": item.get("username"),
            "email": item.get("email"),
            "first_name": item.get("first_name"),
            "last_name": item.get("last_name"),
            "is_active": item.get("is_active"),
            "is_org_admin": item.get("is_org_admin"),
        }

        if return_id:
            processed_item["user_id"] = item.get("id")
        return processed_item

    def _get_proxy_service(self):  # pylint: disable=no-self-use
        """Get proxy service host and port info from environment."""
        proxy_conn_info = {
            PROTOCOL: ENVIRONMENT.get_value("PRINCIPAL_PROXY_SERVICE_PROTOCOL", default="https"),
            HOST: ENVIRONMENT.get_value("PRINCIPAL_PROXY_SERVICE_HOST", default="localhost"),
            PORT: ENVIRONMENT.get_value("PRINCIPAL_PROXY_SERVICE_PORT", default="443"),
            PATH: ENVIRONMENT.get_value("PRINCIPAL_PROXY_SERVICE_PATH", default="/r/insights-services"),
            SOURCE_CERT: ENVIRONMENT.bool("PRINCIPAL_PROXY_SERVICE_SOURCE_CERT", default=False),
            SSL_VERIFY: ENVIRONMENT.bool("PRINCIPAL_PROXY_SERVICE_SSL_VERIFY", default=True),
            USER_ENV: ENVIRONMENT.get_value("PRINCIPAL_PROXY_USER_ENV", default="env"),
            CLIENT_ID: ENVIRONMENT.get_value("PRINCIPAL_PROXY_CLIENT_ID", default="client_id"),
            API_TOKEN: ENVIRONMENT.get_value("PRINCIPAL_PROXY_API_TOKEN", default="token"),
        }
        return proxy_conn_info

    @bop_request_time_tracking.time()
    def _request_principals(
        self,
        url,
        account=None,
        account_filter=False,
        method=requests.get,
        params=None,
        data=None,
        return_id=False,  # noqa: C901
    ):
        """Send request to proxy service."""
        metrics_method = method.__name__.upper()
        if settings.BYPASS_BOP_VERIFICATION:
            to_return = []
            if data is None:
                for principal in Principal.objects.all():
                    to_return.append(
                        dict(
                            username=principal.username,
                            first_name="foo",
                            last_name="bar",
                            email="baz",
                            user_id="51736777",
                        )
                    )
            elif "users" in data:
                for principal in data["users"]:
                    to_return.append(
                        dict(username=principal, first_name="foo", last_name="bar", email="baz", user_id=principal)
                    )
            elif "primaryEmail" in data:
                # We can't fake a lookup for an email address, so we won't try.
                pass
            bop_request_status_count.labels(method=metrics_method, status=200).inc()
            return dict(data=to_return, status_code=200, userCount=len(to_return))
        headers = {USER_ENV_HEADER: self.user_env, CLIENT_ID_HEADER: self.client_id, API_TOKEN_HEADER: self.api_token}
        unexpected_error = {
            "detail": "Unexpected error.",
            "status": str(status.HTTP_500_INTERNAL_SERVER_ERROR),
            "source": "principals",
        }
        try:
            kwargs = {"headers": headers, "params": params, "json": data, "verify": self.ssl_verify}
            if self.source_cert:
                kwargs["verify"] = self.client_cert
            response = method(url, **kwargs)
        except requests.exceptions.ConnectionError as conn:
            LOGGER.error("Unable to connect for URL %s with error: %s", url, conn)
            resp = {"status_code": status.HTTP_500_INTERNAL_SERVER_ERROR, "errors": [unexpected_error]}
            bop_request_status_count.labels(method=metrics_method, status=resp.get("status_code")).inc()
            return resp

        error = None
        resp = {"status_code": response.status_code}
        if response.status_code == status.HTTP_200_OK:
            """Testing if account numbers match"""
            try:
                data = response.json()
                if isinstance(data, dict):
                    userList = self._process_data(data.get("users"), account, account_filter, return_id)
                    resp["data"] = {"userCount": data.get("userCount"), "users": userList}
                else:
                    userList = self._process_data(data, account, account_filter, return_id)
                    resp["data"] = userList
            except ValueError:
                resp["status_code"] = status.HTTP_500_INTERNAL_SERVER_ERROR
                error = unexpected_error
        elif response.status_code == status.HTTP_404_NOT_FOUND:
            error = {"detail": "Not Found.", "status": str(response.status_code), "source": "principals"}
        else:
            LOGGER.error("Error calling URL %s -- status=%d", url, response.status_code)
            error = unexpected_error
            error["status"] = str(response.status_code)
        if error:
            resp["errors"] = [error]
        bop_request_status_count.labels(method=metrics_method, status=resp.get("status_code")).inc()
        return resp

    def request_principals(self, account, input=None, limit=None, offset=None, options={}):
        """Request principals for an account."""
        if input:
            payload = input
            account_principals_path = f"/v1/accounts/{account}/usersBy"
            method = requests.post
        else:
            account_principals_path = f"/v2/accounts/{account}/users"
            method = requests.get
            payload = None

        params = self._create_params(limit, offset, options)
        url = "{}://{}:{}{}{}".format(self.protocol, self.host, self.port, self.path, account_principals_path)

        # For v2 account users endpoints are already filtered by account
        return self._request_principals(url, params=params, account_filter=False, method=method, data=payload)

    def request_filtered_principals(self, principals, account=None, limit=None, offset=None, options={}):
        """Request specific principals for an account."""
        if account is None:
            account_filter = False
        else:
            account_filter = True
        if not principals:
            return {"status_code": status.HTTP_200_OK, "data": []}
        filtered_principals_path = "/v1/users"
        params = self._create_params(limit, offset, options)
        payload = {"users": principals, "include_permissions": False}
        url = "{}://{}:{}{}{}".format(self.protocol, self.host, self.port, self.path, filtered_principals_path)

        return_id = False if options.get("return_id") is None else True
        return self._request_principals(
            url,
            account=account,
            account_filter=account_filter,
            method=requests.post,
            params=params,
            data=payload,
            return_id=return_id,
        )
