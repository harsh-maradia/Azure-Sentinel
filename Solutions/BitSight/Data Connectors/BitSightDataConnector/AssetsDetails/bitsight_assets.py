"""Module containing the BitSightAssets class for fetching BitSight assets data and posting it to Sentinel."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from ..SharedCode import consts
from ..SharedCode.bitsight_client import BitSight
from ..SharedCode.bitsight_exception import BitSightException, BitSightTimeOutException
from ..SharedCode.get_logs_data import get_logs_data
from ..SharedCode.logger import applogger
from ..SharedCode.utils import CheckpointManager

DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ENTRY_SEPARATOR = ":"


def canonical(value):
    """Sort every list of a value recursively, so that a different array order is not seen as a change.

    Args:
        value (any): Value to be made canonical.

    Returns:
        any: Value having all the lists sorted.
    """
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return sorted(
            (canonical(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    return value


def get_asset_hashes(asset):
    """Get the identity hash and the content hash of an asset.

    The identity hash is made from the asset type and the asset name. The content hash is made from the whole
    record except temporary_id, which changes between the API calls.

    Args:
        asset (dict): Asset returned by the API.

    Returns:
        tuple: Asset key and content hash.
    """
    identity = "{}|{}".format(asset.get("asset_type"), asset.get("asset")).lower()
    asset_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[: consts.ASSETS_HASH_LENGTH]
    record = {key: value for key, value in asset.items() if key != "temporary_id"}
    content_hash = hashlib.sha512(
        json.dumps(canonical(record), sort_keys=True).encode("utf-8")
    ).hexdigest()[: consts.ASSETS_HASH_LENGTH]
    return asset_key, content_hash


class AssetHashSet:
    """Insertion ordered map of asset key to content hash, which is stored in chunks of fixed size.

    Chunk i of the set always holds the entries i * chunk size to (i + 1) * chunk size - 1 in insertion order, so
    only the chunks changed since the last save have to be written again.
    """

    def __init__(self) -> None:
        """Initialize an empty set."""
        self.hashes = {}
        self.keys = []
        self.positions = {}
        self.dirty_chunks = set()

    def __len__(self):
        """Get the number of assets in the set."""
        return len(self.hashes)

    def __contains__(self, asset_key):
        """Check if the asset is in the set."""
        return asset_key in self.hashes

    def get(self, asset_key):
        """Get the content hash of an asset, None if it is not in the set."""
        return self.hashes.get(asset_key)

    def set(self, asset_key, content_hash):
        """Add an asset or change its content hash and mark its chunk as changed."""
        if asset_key in self.hashes:
            if self.hashes[asset_key] == content_hash:
                return
        else:
            self.positions[asset_key] = len(self.keys)
            self.keys.append(asset_key)
        self.hashes[asset_key] = content_hash
        self.dirty_chunks.add(self.positions[asset_key] // consts.ASSETS_CHUNK_SIZE)

    def load_entry(self, entry):
        """Add a stored entry without marking its chunk as changed."""
        asset_key, _, content_hash = entry.partition(ENTRY_SEPARATOR)
        if asset_key not in self.hashes:
            self.positions[asset_key] = len(self.keys)
            self.keys.append(asset_key)
        self.hashes[asset_key] = content_hash

    def chunk_count(self):
        """Get the number of chunks needed to store the set."""
        return -(-len(self.hashes) // consts.ASSETS_CHUNK_SIZE)

    def get_chunk(self, chunk_index):
        """Get the stored entries of a chunk.

        Args:
            chunk_index (int): Zero based index of the chunk.

        Returns:
            list: Entries of the chunk in "asset_key:content_hash" format.
        """
        start = chunk_index * consts.ASSETS_CHUNK_SIZE
        return [
            asset_key + ENTRY_SEPARATOR + self.hashes[asset_key]
            for asset_key in self.keys[start: start + consts.ASSETS_CHUNK_SIZE]
        ]


class BitSightAssets(BitSight):
    """Class for fetching BitSight assets data and posting it to Sentinel.

    The hashes of the assets of every company are stored in the checkpoint table in chunks, per generation:
        <company_guid>-state          : state of the company
        <company_guid>-<gen>-<chunk>  : chunk of the asset hashes of a generation

    A run compares the assets returned by the API with the live generation and posts only the new, updated and removed
    assets. The assets seen in the current cycle are written as the next (pending) generation, and the cycle is
    committed by changing the generation in the state row. Because of that, a company too large for one run
    resumes in the next run and a crash never leaves a mixed up checkpoint.
    """

    def __init__(self, start_time) -> None:
        """Initialize BitSightAssets object.

        Args:
            start_time (float): The start time for data fetching.
        """
        super().__init__(start_time)
        self.start_time = start_time
        self.check_env_var = self.check_environment_var_exist(
            [
                {"api_token": consts.API_TOKEN},
                {"Assets_Table_Name": consts.ASSETS_TABLE_NAME},
                {"companies_list": consts.COMPANIES},
            ]
        )
        self.checkpoint_obj = CheckpointManager(
            connection_string=consts.CONN_STRING,
            table_name=consts.ASSETS_CHECKPOINT_TABLE
        )
        self.refresh_hours = self.get_refresh_hours()
        self.assets_endpoint_path = consts.ENDPOINTS["assets_endpoint_path"]
        self.generate_auth_token()

    def get_refresh_hours(self):
        """Get the number of hours after which the assets of a company are fetched again.

        Returns:
            int: Refresh interval in hours.
        """
        try:
            refresh_hours = int(consts.ASSETS_REFRESH_HOURS)
            if refresh_hours < 0:
                raise ValueError("negative value")
            return refresh_hours
        except (TypeError, ValueError):
            applogger.warning(
                "{} {} Invalid Assets_Refresh_Hours value '{}', using default {} hours.".format(
                    self.logs_starts_with,
                    consts.ASSETS_FUNC_NAME,
                    consts.ASSETS_REFRESH_HOURS,
                    consts.DEFAULT_ASSETS_REFRESH_HOURS,
                )
            )
            return consts.DEFAULT_ASSETS_REFRESH_HOURS

    def get_assets_data_into_sentinel(self) -> None:
        """Fetch assets data for all companies or specified companies and post it to Sentinel."""
        try:
            if not self.check_env_var:
                raise BitSightException(
                    "{} {} Some Environment variables are not set hence exiting the app.".format(
                        self.logs_starts_with, consts.ASSETS_FUNC_NAME
                    )
                )

            applogger.info(
                "{} {} Fetching companies from companies table.".format(
                    self.logs_starts_with, consts.ASSETS_FUNC_NAME
                )
            )
            logs_data, flag = get_logs_data()
            if not flag:
                applogger.info(
                    "{} {} Companies are not available yet.".format(
                        self.logs_starts_with, consts.ASSETS_FUNC_NAME
                    )
                )
                return

            applogger.info(
                "{} {} Fetched companies from companies table.".format(
                    self.logs_starts_with, consts.ASSETS_FUNC_NAME
                )
            )
            logs_data = sorted(logs_data, key=lambda x: x["name"])
            company_names = [data["name"] for data in logs_data]

            if consts.COMPANIES.strip().lower() == "all":
                companies = logs_data
            else:
                companies_to_get = set(
                    self.get_specified_companies_list(company_names, consts.COMPANIES)
                )
                companies = [
                    data for data in logs_data if data["name"].lower() in companies_to_get
                ]

            if not companies:
                applogger.info(
                    "{} {} No companies found to fetch assets.".format(
                        self.logs_starts_with, consts.ASSETS_FUNC_NAME
                    )
                )
                return
            self.get_companies_assets_details(companies)
        except BitSightTimeOutException:
            applogger.error(
                "{} {} 9:00 mins executed hence stopping the function app.".format(
                    self.logs_starts_with, consts.ASSETS_FUNC_NAME
                )
            )
            return
        except BitSightException:
            raise BitSightException()
        except Exception as err:
            applogger.exception(
                "{} {} Error: {}".format(
                    self.logs_starts_with, consts.ASSETS_FUNC_NAME, err
                )
            )
            raise BitSightException()

    def get_ordered_companies(self, companies):
        """Order companies to start right after the last processed company.

        Args:
            companies (list): List of company dicts having name and guid, sorted by name.

        Returns:
            list: Companies starting after the last processed company guid.
        """
        last_company_guid = self.checkpoint_obj.get_checkpoint(
            partition_key=consts.COMPANY_CHECKPOINT_PARTITION_KEY,
            row_key=consts.COMPANY_CHECKPOINT_ROW_KEY
        )
        company_guids = [data.get("guid") for data in companies]
        start_index = 0
        if last_company_guid in company_guids:
            start_index = company_guids.index(last_company_guid) + 1
        applogger.debug(
            "{} {} Started data fetching from index {}.".format(
                self.logs_starts_with, consts.ASSETS_FUNC_NAME, start_index % len(companies)
            )
        )
        return companies[start_index:] + companies[:start_index]

    def get_companies_assets_details(self, companies):
        """Fetch assets data for the given companies and post it to Sentinel.

        Args:
            companies (list): List of company dicts having name and guid.
        """
        count_companies = 0
        try:
            for company in self.get_ordered_companies(companies):
                self.check_timeout()
                company_name = company.get("name")
                company_guid = company.get("guid")
                try:
                    if self.get_assets_details(company_name, company_guid):
                        count_companies += 1
                except BitSightException:
                    applogger.error(
                        "{} {} Error while fetching assets of {} company, it will be retried in the next run.".format(
                            self.logs_starts_with, consts.ASSETS_FUNC_NAME, company_name
                        )
                    )
                self.checkpoint_obj.set_checkpoint(
                    partition_key=consts.COMPANY_CHECKPOINT_PARTITION_KEY,
                    row_key=consts.COMPANY_CHECKPOINT_ROW_KEY,
                    value=company_guid
                )
            applogger.info(
                "{} {} Completed assets of {} companies.".format(
                    self.logs_starts_with, consts.ASSETS_FUNC_NAME, count_companies
                )
            )
        except BitSightTimeOutException:
            applogger.info(
                "{} {} Completed assets of {} companies before timeout.".format(
                    self.logs_starts_with, consts.ASSETS_FUNC_NAME, count_companies
                )
            )
            raise BitSightTimeOutException()
        except BitSightException:
            raise BitSightException()
        except Exception as err:
            applogger.exception(
                "{} {} Error: {}".format(
                    self.logs_starts_with, consts.ASSETS_FUNC_NAME, err
                )
            )
            raise BitSightException()

    @staticmethod
    def get_state_row_key(company_guid):
        """Get the row key of the state of a company."""
        return "{}-state".format(company_guid)

    @staticmethod
    def get_chunk_row_key(company_guid, generation, chunk_number):
        """Get the row key of a chunk of the asset hashes of a company."""
        return "{}-{}-{}".format(company_guid, generation, chunk_number)

    @staticmethod
    def get_current_time():
        """Get the current UTC time in the checkpoint format."""
        return datetime.now(timezone.utc).strftime(DATETIME_FORMAT)

    @staticmethod
    def parse_time(value):
        """Parse a time stored in the checkpoint, None if it is not available or invalid."""
        try:
            return datetime.strptime(value, DATETIME_FORMAT).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    def load_state(self, company_guid):
        """Get the state of a company from the checkpoint table.

        Args:
            company_guid (str): GUID of the company.

        Returns:
            dict: State of the company, the initial state if it is missing or invalid.
        """
        state = {
            "gen": 0,
            "chunks": 0,
            "status": None,
            "last_completed": None,
            "pending_gen": None,
            "pending_chunks": 0,
            "offset": 0,
            "limit": None,
            "last_forbidden": None,
        }
        state_value = self.checkpoint_obj.get_checkpoint(
            partition_key=consts.DATA_CHECKPOINT_PARTITION_KEY,
            row_key=self.get_state_row_key(company_guid)
        )
        if not state_value:
            return state
        try:
            state.update(json.loads(state_value))
        except (json.JSONDecodeError, TypeError):
            applogger.warning(
                "{} {} Invalid state for company guid {}, starting a new cycle.".format(
                    self.logs_starts_with, consts.ASSETS_FUNC_NAME, company_guid
                )
            )
        return state

    def save_state(self, company_guid, state):
        """Save the state of a company in the checkpoint table.

        Args:
            company_guid (str): GUID of the company.
            state (dict): State of the company.
        """
        self.checkpoint_obj.set_checkpoint(
            partition_key=consts.DATA_CHECKPOINT_PARTITION_KEY,
            row_key=self.get_state_row_key(company_guid),
            value=json.dumps(state, separators=(",", ":"))
        )

    def is_company_due(self, state):
        """Check if the assets of a company have to be fetched in this run.

        Args:
            state (dict): State of the company.

        Returns:
            bool: False if the company was refreshed or forbidden within the refresh interval.
        """
        refresh_interval = timedelta(hours=self.refresh_hours)
        now = datetime.now(timezone.utc)
        last_forbidden = self.parse_time(state.get("last_forbidden"))
        if last_forbidden and now - last_forbidden < refresh_interval:
            return False
        last_completed = self.parse_time(state.get("last_completed"))
        if state.get("status") == consts.ASSETS_STATUS_COMPLETE and last_completed:
            return now - last_completed >= refresh_interval
        return True

    def load_generation(self, company_guid, generation, chunks):
        """Load the asset hashes of a generation from the checkpoint table.

        Args:
            company_guid (str): GUID of the company.
            generation (int): Generation to load.
            chunks (int): Number of chunks of the generation.

        Returns:
            AssetHashSet: Asset hashes, None if a chunk is missing or invalid.
        """
        hash_set = AssetHashSet()
        if not chunks:
            return hash_set
        rows = self.checkpoint_obj.get_checkpoints_by_prefix(
            consts.DATA_CHECKPOINT_PARTITION_KEY,
            "{}-{}-".format(company_guid, generation),
        )
        try:
            for chunk_number in range(1, chunks + 1):
                chunk_value = rows.get(self.get_chunk_row_key(company_guid, generation, chunk_number))
                if chunk_value is None:
                    raise ValueError("chunk {} is missing".format(chunk_number))
                for entry in json.loads(chunk_value):
                    hash_set.load_entry(entry)
        except (ValueError, TypeError) as err:
            applogger.error(
                "{} {} Invalid checkpoint of company guid {} generation {}: {}".format(
                    self.logs_starts_with, consts.ASSETS_FUNC_NAME, company_guid, generation, err
                )
            )
            return None
        return hash_set

    def save_dirty_chunks(self, company_guid, generation, hash_set):
        """Save the chunks of the asset hashes which are changed since the last save.

        Args:
            company_guid (str): GUID of the company.
            generation (int): Generation to save.
            hash_set (AssetHashSet): Asset hashes.
        """
        for chunk_index in sorted(hash_set.dirty_chunks):
            self.checkpoint_obj.set_checkpoint(
                partition_key=consts.DATA_CHECKPOINT_PARTITION_KEY,
                row_key=self.get_chunk_row_key(company_guid, generation, chunk_index + 1),
                value=json.dumps(hash_set.get_chunk(chunk_index), separators=(",", ":"))
            )
        hash_set.dirty_chunks.clear()

    def sweep_checkpoints(self, company_guid, generation, chunks):
        """Delete the chunks of the company which are not part of the live generation.

        Args:
            company_guid (str): GUID of the company.
            generation (int): Live generation.
            chunks (int): Number of chunks of the live generation.
        """
        try:
            rows = self.checkpoint_obj.get_checkpoints_by_prefix(
                consts.DATA_CHECKPOINT_PARTITION_KEY, "{}-".format(company_guid)
            )
            live_keys = {
                self.get_chunk_row_key(company_guid, generation, chunk_number)
                for chunk_number in range(1, chunks + 1)
            }
            live_keys.add(self.get_state_row_key(company_guid))
            for row_key in rows:
                if row_key not in live_keys:
                    self.checkpoint_obj.delete_checkpoint(consts.DATA_CHECKPOINT_PARTITION_KEY, row_key)
        except BitSightException:
            applogger.warning(
                "{} {} Could not delete the old checkpoints of company guid {}, they will be deleted in the "
                "next cycle.".format(self.logs_starts_with, consts.ASSETS_FUNC_NAME, company_guid)
            )

    def get_assets_page(self, url, offset, limit):
        """Fetch one page of assets.

        A new cycle starts with the default page size. If a request fails at any page, the same page
        is requested again with the fallback page size, which is then used for the rest of the cycle.
        The page size is saved in the company state, so it is also kept when the cycle is resumed in
        the next run, and it is cleared when the cycle completes.

        Args:
            url (str): Assets endpoint url of the company.
            offset (int): Offset of the page.
            limit (int): Page size, None if not known yet.

        Returns:
            tuple: Response of the API (None if the company is not accessible) and page size used.

        Raises:
            BitSightException: Raises exception if the request fails with the fallback page size.
        """
        limit = limit or consts.ASSETS_PAGE_SIZE
        try:
            response = self.get_bitsight_data(url, {"limit": limit, "offset": offset})
            return response, limit
        except BitSightException:
            if limit <= consts.ASSETS_FALLBACK_PAGE_SIZE:
                raise
            applogger.warning(
                "{} {} Request with page size {} failed at offset {}, using page size {} for the rest of "
                "this cycle.".format(
                    self.logs_starts_with,
                    consts.ASSETS_FUNC_NAME,
                    limit,
                    offset,
                    consts.ASSETS_FALLBACK_PAGE_SIZE,
                )
            )
            limit = consts.ASSETS_FALLBACK_PAGE_SIZE
            response = self.get_bitsight_data(url, {"limit": limit, "offset": offset})
            return response, limit

    def get_assets_details(self, company_name, company_guid):
        """Fetch assets of a specific company and post the new, updated and removed assets to Sentinel.

        Args:
            company_name (str): Name of the company.
            company_guid (str): GUID of the company.

        Returns:
            bool: True if the cycle of the company is completed in this run.

        Raises:
            BitSightException: Raises exception if any error occurs.
        """
        try:
            state = self.load_state(company_guid)
            if not self.is_company_due(state):
                applogger.info(
                    "{} {} Assets of {} company are not due for refresh yet.".format(
                        self.logs_starts_with, consts.ASSETS_FUNC_NAME, company_name
                    )
                )
                return False

            committed = self.load_generation(company_guid, state["gen"], state["chunks"])
            if committed is None:
                applogger.warning(
                    "{} {} Checkpoint of {} company is invalid, all its assets will be posted again.".format(
                        self.logs_starts_with, consts.ASSETS_FUNC_NAME, company_name
                    )
                )
                committed = AssetHashSet()

            seen = None
            if state["status"] == consts.ASSETS_STATUS_IN_PROGRESS:
                pending_generation = state["pending_gen"]
                seen = self.load_generation(company_guid, pending_generation, state["pending_chunks"])
                if seen is not None:
                    offset = int(state["offset"])
                    limit = state["limit"]
                    applogger.info(
                        "{} {} Resuming assets of {} company from offset {}.".format(
                            self.logs_starts_with, consts.ASSETS_FUNC_NAME, company_name, offset
                        )
                    )
            if seen is None:
                pending_generation = state["gen"] + 1
                seen = AssetHashSet()
                offset = 0
                limit = None
                applogger.info(
                    "{} {} Starting assets cycle of {} company.".format(
                        self.logs_starts_with, consts.ASSETS_FUNC_NAME, company_name
                    )
                )

            assets_url = self.base_url + self.assets_endpoint_path.format(company_guid)
            while True:
                self.check_timeout()
                response, limit = self.get_assets_page(assets_url, offset, limit)
                if response is None:
                    state["last_forbidden"] = self.get_current_time()
                    self.save_state(company_guid, state)
                    return False
                results = response.get("results") or []
                next_link = (response.get("links") or {}).get("next")

                changed_assets = self.get_changed_assets(
                    results, committed, seen, company_name, company_guid
                )
                if changed_assets:
                    self.send_data_to_sentinel(
                        changed_assets, consts.ASSETS_TABLE_NAME, company_name, "assets"
                    )
                offset += len(results)
                applogger.debug(
                    "{} {} Fetched {} assets of {} company, posted {}, offset {}.".format(
                        self.logs_starts_with,
                        consts.ASSETS_FUNC_NAME,
                        len(results),
                        company_name,
                        len(changed_assets),
                        offset,
                    )
                )

                if not next_link or not results:
                    self.complete_cycle(
                        company_name, company_guid, state, committed, seen, pending_generation,
                        response.get("count"),
                    )
                    return True

                self.save_dirty_chunks(company_guid, pending_generation, seen)
                state.update(
                    {
                        "status": consts.ASSETS_STATUS_IN_PROGRESS,
                        "pending_gen": pending_generation,
                        "pending_chunks": seen.chunk_count(),
                        "offset": offset,
                        "limit": limit,
                        "last_forbidden": None,
                    }
                )
                self.save_state(company_guid, state)
        except BitSightTimeOutException:
            raise BitSightTimeOutException()
        except BitSightException:
            raise BitSightException()
        except Exception as err:
            applogger.exception(
                "{} {} Error: {}".format(
                    self.logs_starts_with, consts.ASSETS_FUNC_NAME, err
                )
            )
            raise BitSightException()

    def get_changed_assets(self, results, committed, seen, company_name, company_guid):
        """Find the new and updated assets of a page and add them to the assets seen in the current cycle.

        Args:
            results (list): Assets of a page.
            committed (AssetHashSet): Asset hashes of the live generation.
            seen (AssetHashSet): Asset hashes seen in the current cycle.
            company_name (str): Name of the company.
            company_guid (str): GUID of the company.

        Returns:
            list: Assets to post.
        """
        changed_assets = []
        for asset in results:
            asset_key, content_hash = get_asset_hashes(asset)
            committed_hash = committed.get(asset_key)
            if committed_hash != content_hash and seen.get(asset_key) != content_hash:
                asset["company_name"] = company_name
                asset["company_guid"] = company_guid
                asset["asset_key"] = asset_key
                asset["content_hash"] = content_hash
                asset["change_type"] = (
                    consts.ASSETS_CHANGE_NEW if committed_hash is None else consts.ASSETS_CHANGE_UPDATED
                )
                changed_assets.append(asset)
            seen.set(asset_key, content_hash)
        return changed_assets

    def complete_cycle(
        self, company_name, company_guid, state, committed, seen, pending_generation, assets_count
    ):
        """Post the removed assets, make the assets seen in the cycle as the live generation and clean up.

        If nothing is changed in the cycle, the live generation is kept and only the state is updated.

        Args:
            company_name (str): Name of the company.
            company_guid (str): GUID of the company.
            state (dict): State of the company.
            committed (AssetHashSet): Asset hashes of the live generation.
            seen (AssetHashSet): Asset hashes seen in the current cycle.
            pending_generation (int): Generation to make live.
            assets_count (int): Number of assets as returned by the API.
        """
        unseen_assets = [
            (asset_key, content_hash)
            for asset_key, content_hash in committed.hashes.items()
            if asset_key not in seen
        ]
        if len(seen) == assets_count:
            removed_assets = [
                {
                    "company_name": company_name,
                    "company_guid": company_guid,
                    "asset_key": asset_key,
                    "content_hash": content_hash,
                    "change_type": consts.ASSETS_CHANGE_REMOVED,
                }
                for asset_key, content_hash in unseen_assets
            ]
            if removed_assets:
                self.send_data_to_sentinel(
                    removed_assets, consts.ASSETS_TABLE_NAME, company_name, "assets"
                )
        else:
            applogger.warning(
                "{} {} {} assets of {} company are fetched but BitSight count is {}, removed assets are not "
                "checked in this cycle.".format(
                    self.logs_starts_with, consts.ASSETS_FUNC_NAME, len(seen), company_name, assets_count
                )
            )
            for asset_key, content_hash in unseen_assets:
                seen.set(asset_key, content_hash)

        if seen.hashes == committed.hashes:
            generation, chunks = state["gen"], state["chunks"]
        else:
            self.save_dirty_chunks(company_guid, pending_generation, seen)
            generation, chunks = pending_generation, seen.chunk_count()
        state.update(
            {
                "gen": generation,
                "chunks": chunks,
                "status": consts.ASSETS_STATUS_COMPLETE,
                "last_completed": self.get_current_time(),
                "pending_gen": None,
                "pending_chunks": 0,
                "offset": 0,
                "limit": None,
                "last_forbidden": None,
            }
        )
        self.save_state(company_guid, state)
        self.sweep_checkpoints(company_guid, generation, chunks)
        applogger.info(
            "{} {} Completed assets cycle of {} company, {} assets, BitSight count {}.".format(
                self.logs_starts_with, consts.ASSETS_FUNC_NAME, company_name, len(seen), assets_count
            )
        )
