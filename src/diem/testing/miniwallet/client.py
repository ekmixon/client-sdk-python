# Copyright (c) The Diem Core Contributors
# SPDX-License-Identifier: Apache-2.0

from aiohttp import ClientSession, ClientError, ClientResponseError
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Any, Dict, Callable
from .app import KycSample, Event
from .app.store import _match
from ... import offchain, jsonrpc
import logging, json, os


@dataclass
class Payment:
    id: str
    account_id: str
    currency: str
    amount: int
    payee: str


@dataclass
class RestClient:
    name: str
    server_url: str
    logger: logging.Logger = field(init=False)
    events_api_is_optional: bool = field(default=False)
    session_factory: Callable[[], ClientSession] = ClientSession

    def __post_init__(self) -> None:
        self.logger = logging.getLogger(self.name)

    async def random_account_identifier(self) -> str:
        account = await self.create_account()
        return await account.generate_account_identifier()

    async def create_account(
        self,
        balances: Optional[Dict[str, int]] = None,
        kyc_data: Optional[offchain.KycDataObject] = None,
        reject_additional_kyc_data_request: Optional[bool] = None,
        disable_background_tasks: Optional[bool] = None,
    ) -> "AccountResource":
        kwargs = {
            "balances": balances,
            "kyc_data": asdict(kyc_data) if kyc_data else None,
            "reject_additional_kyc_data_request": reject_additional_kyc_data_request,
            "disable_background_tasks": disable_background_tasks,
        }
        account = await self.create("/accounts", **{k: v for k, v in kwargs.items() if v})
        return AccountResource(client=self, id=account["id"], kyc_data=kyc_data)

    async def get_kyc_sample(self) -> KycSample:
        return offchain.from_dict(await self.send("GET", "/kyc_sample"), KycSample)

    async def create(self, path: str, **kwargs: Any) -> Dict[str, Any]:
        return await self.send("POST", path, json.dumps(kwargs) if kwargs else None)

    async def get(self, path: str) -> Dict[str, Any]:
        return await self.send("GET", path)

    # pyre-ignore
    async def send(self, method: str, path: str, data: Optional[str] = None, return_text: bool = False) -> Any:
        url = f'{self.server_url.rstrip("/")}/{path.lstrip("/")}'
        self.logger.debug("%s %s: %s", method, path, data)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": jsonrpc.USER_AGENT_HTTP_HEADER,
        }
        if tc := os.getenv("PYTEST_CURRENT_TEST"):
            headers["X-Test-Case"] = tc.split("::")[-1]
        async with self.session_factory() as session:
            async with session.request(method, url.lower(), data=data, headers=headers) as resp:
                log_level = logging.DEBUG if resp.status < 300 else logging.ERROR
                if self.events_api_is_optional and path.endswith("/events"):
                    log_level = logging.DEBUG

                self.logger.log(log_level, "%s %s: %s - %s", method, path, data, resp.status)
                body = await resp.text()
                self.logger.log(log_level, "response body: \n%s", try_json(body))
                try:
                    resp.raise_for_status()
                except ClientResponseError as e:
                    e.message += "\n" + body
                    raise e
                return body if return_text else json.loads(body)


def try_json(text: str) -> str:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            # pretty print error json stacktrace info
            return "\n".join([f"{k}: {v}" for k, v in obj.items()])
        return json.dumps(obj, indent=2)
    except Exception:
        return text


@dataclass
class AccountResource:

    client: RestClient
    id: str
    kyc_data: Optional[offchain.KycDataObject] = field(default=None)

    async def balance(self, currency: str) -> int:
        """Get account balance for the given currency

        Calls `GET /accounts/{account_id}/balances` endpoint and only return balance of the given currency.
        Returns 0 if given currency does not exist in the returned balances.
        """

        return (await self.balances()).get(currency, 0)

    async def send_payment(self, currency: str, amount: int, payee: str) -> Payment:
        """Send amount of currency to payee

        Calls `POST /accounts/{account_id}/payments` endpoint and returns payment details.
        """

        p = await self.client.create(self._resources("payment"), payee=payee, currency=currency, amount=amount)
        return Payment(id=p["id"], account_id=self.id, payee=payee, currency=currency, amount=amount)

    async def generate_account_identifier(self) -> str:
        """Generate an account identifier

        Calls `POST /accounts/{account_id}/account_identifiers` to generate account identifier.
        """

        ret = await self.client.create(self._resources("account_identifier"))
        return ret["account_identifier"]

    async def events(self, start: int = 0) -> List[Event]:
        """Get account events

        Calls to `GET /accounts/{account_id}/events` endpoint and returns events list.

        Raises `aiohttp.ClientResponseError`, if the endpoint is not implemented.
        """

        ret = await self.client.send("GET", self._resources("event"))
        return [Event(**obj) for obj in ret[start:]]

    async def find_event(self, event_type: str, start_index: int = 0, **kwargs: Any) -> Optional[Event]:
        """Find a specific event by `type`, `start_index` and `data`

        When matching the event `data`, it assumes `data` is JSON encoded dictionary, and
        returns the event if the `**kwargs` is subset of the dictionary decoded from event `data` field.
        """

        events = await self.events(start_index)
        events = [e for e in events if e.type == event_type]
        for e in events:
            if _match(json.loads(e.data), **kwargs):
                return e

    async def log_events(self) -> None:
        """Log account events as INFO

        Does nothing if get events API is not implemented.
        """

        events = await self.dump_events()
        if events:
            self.client.logger.info("account(%s) events: %s", self.id, events)

    async def dump_events(self) -> str:
        """Dump account events as JSON encoded string (well formatted, and indent=2)

        Returns empty string if get events API is not implemented.
        """

        try:
            return json.dumps(list(map(self.event_asdict, await self.events())), indent=2)
        except ClientError:
            return ""

    def event_asdict(self, event: Event) -> Dict[str, Any]:
        """Returns `Event` as dictionary object.

        As we use JSON-encoded string field, this function tries to decoding all JSON-encoded
        string as dictionary for pretty print event data in log.
        """

        ret = asdict(event)
        try:
            ret["data"] = json.loads(event.data)
        except json.decoder.JSONDecodeError:
            pass
        return ret

    def info(self, *args: Any, **kwargs: Any) -> None:
        """Log info to `client.logger`"""

        self.client.logger.info(*args, **kwargs)

    async def balances(self) -> Dict[str, int]:
        """returns account balances object

        should always prefer to use func `balance(currency) -> int`, which returns zero
        when currency not exist in the response.
        """

        return await self.client.get(self._resources("balance"))

    def _resources(self, resource: str) -> str:
        return f"/accounts/{self.id}/{resource}s"
