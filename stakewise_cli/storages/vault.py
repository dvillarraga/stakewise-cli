import collections
import copy
import json
import time
from functools import cached_property, lru_cache
from typing import Dict, OrderedDict, Set

import click
from eth_typing import BLSPubkey, ChecksumAddress, HexStr
from eth_utils import add_0x_prefix
from hvac import Client as VaultClient
from hvac.exceptions import InvalidPath
from py_ecc.bls import G2ProofOfPossession
from staking_deposit.key_handling.keystore import ScryptKeystore
from web3 import Web3
from web3.beacon import Beacon

from stakewise_cli.eth1 import (
    get_operator_deposit_data_ipfs_link,
    is_validator_registered,
)
from stakewise_cli.eth2 import (
    COIN_TYPE,
    EXITED_STATUSES,
    PURPOSE,
    generate_password,
    get_mnemonic_signing_key,
    get_validators,
)
from stakewise_cli.ipfs import ipfs_fetch
from stakewise_cli.networks import NETWORKS
from stakewise_cli.queries import get_ethereum_gql_client, get_stakewise_gql_client
from stakewise_cli.settings import IS_LEGACY, VAULT_VALIDATORS_MOUNT_POINT
from stakewise_cli.typings import SigningKey, VaultKeystore, VaultState

VALIDATOR_POLICY = """
path "%s/%s/*" {
  capabilities = ["read", "list"]
}
"""


def generate_validator_name(validator_names: Set[str]) -> str:
    """Generates unique validator name."""
    index = 0
    name = f"validator{index}"
    while name in validator_names:
        index += 1
        name = f"validator{index}"

    return name


class Vault(object):
    def __init__(
        self,
        vault_client: VaultClient,
        beacon: Beacon,
        operator: ChecksumAddress,
        network: str,
        mnemonic: str,
        namespace: str,
    ):
        self.vault_client = vault_client
        self.network = network
        self.sw_gql_client = get_stakewise_gql_client(network)
        self.eth_gql_client = get_ethereum_gql_client(network)
        self.beacon = beacon
        self.mnemonic = mnemonic
        self.namespace = namespace
        self.max_keys_per_validator = NETWORKS[network]["MAX_KEYS_PER_VALIDATOR"]
        self.operator_address = operator
        self.check_mnemonic()
        global VAULT_VALIDATORS_MOUNT_POINT
        if VAULT_VALIDATORS_MOUNT_POINT == "":
            VAULT_VALIDATORS_MOUNT_POINT = namespace

    @cached_property
    def vault_validator_names(self) -> Set[str]:
        """Fetches names of vault validators."""
        try:
            names = self.vault_client.secrets.kv.list_secrets(
                path="", mount_point=VAULT_VALIDATORS_MOUNT_POINT
            )["data"]["keys"]
            return set([name.strip("/") for name in names])
        except InvalidPath:
            return set()

    @cached_property
    def vault_current_state(self) -> VaultState:
        """Returns mappings of vault public keys to keystores."""
        result: VaultState = {}
        with click.progressbar(
            self.vault_validator_names,
            label="Fetching vault current state\t\t",
            show_percent=False,
            show_pos=True,
        ) as validator_names:
            for validator_name in validator_names:
                try:
                    validator_keystores: Dict[
                        str, str
                    ] = self.vault_client.secrets.kv.read_secret(
                        path=f"{validator_name}/keystores",
                        mount_point=VAULT_VALIDATORS_MOUNT_POINT,
                    )[
                        "data"
                    ]
                except InvalidPath:
                    continue

                for keystore_str in validator_keystores.values():
                    keystore = json.loads(keystore_str)
                    public_key = add_0x_prefix(HexStr(keystore["pubkey"]))
                    if public_key in result:
                        raise click.ClickException(
                            f"Public key {public_key} is presented in {validator_name}"
                            f" and {result[public_key]} vault validators."
                            f" You must immediately stop both validators to avoid slashing!"
                        )
                    result[public_key] = VaultKeystore(
                        validator_name=validator_name, keystore=keystore_str
                    )

        return result

    @cached_property
    def operator_deposit_data_public_keys(self) -> Set[HexStr]:
        """Returns operator's deposit data public keys."""
        deposit_data_ipfs_link = get_operator_deposit_data_ipfs_link(
            self.sw_gql_client, self.operator_address
        )
        result: Set[HexStr] = set()
        if not deposit_data_ipfs_link:
            return result

        deposit_datum = ipfs_fetch(deposit_data_ipfs_link)
        for deposit_data in deposit_datum:
            public_key = deposit_data["public_key"]
            if public_key in result:
                raise click.ClickException(
                    f"Public key {public_key} is presented twice in {deposit_data_ipfs_link}"
                )
            result.add(public_key)

        return result

    @cached_property
    def vault_missing_keypairs(self) -> OrderedDict[HexStr, SigningKey]:
        """
        Returns ordered mapping of BLS public key to private key
        that are in deposit data or active but are missing in the vault.
        """

        missed_keypairs: OrderedDict[HexStr, SigningKey] = collections.OrderedDict()

        from_index = 0
        while True:
            signing_key = get_mnemonic_signing_key(self.mnemonic, from_index, IS_LEGACY)
            public_key = Web3.toHex(G2ProofOfPossession.SkToPk(signing_key.key))

            if public_key in self.vault_current_state:
                from_index += 1
                continue

            if public_key in self.operator_deposit_data_public_keys:
                missed_keypairs[public_key] = signing_key
                from_index += 1
                continue

            is_registered = is_validator_registered(
                gql_client=self.eth_gql_client, public_key=public_key
            )
            if is_registered:
                missed_keypairs[public_key] = signing_key
                from_index += 1
                continue

            break

        if not missed_keypairs:
            return missed_keypairs

        exited_public_keys: Set[HexStr] = set()
        missed_keypairs_items = list(missed_keypairs.items())
        missed_keypairs_count = len(missed_keypairs_items)
        with click.progressbar(
            length=missed_keypairs_count,
            label="Checking vault missing keys statuses\t\t",
            show_percent=False,
            show_pos=True,
        ) as bar:
            for i in range(0, missed_keypairs_count, 100):
                keypairs_chunk = missed_keypairs_items[i : i + 100]
                validators = get_validators(
                    beacon=self.beacon,
                    public_keys=[HexStr(keypair[0]) for keypair in keypairs_chunk],
                    state_id="finalized",
                )
                for validator in validators:
                    if validator["status"] in EXITED_STATUSES:
                        public_key = validator["validator"]["pubkey"]
                        exited_public_keys.add(public_key)

                bar.update(len(keypairs_chunk))

        for public_key in exited_public_keys:
            del missed_keypairs[public_key]

        click.confirm(
            click.style(
                f"Fetched {len(missed_keypairs)} missing validator keys. Upload them to vault?",
                fg="green",
            ),
            abort=True,
        )

        return missed_keypairs

    @cached_property
    def operator_exited_public_keys(self) -> Set[HexStr]:
        """Returns operator's public keys that have been exited but are still in the vault."""
        result: Set[HexStr] = set()

        # fetch validators in chunks of 100 keys
        all_public_keys = list(self.vault_current_state.keys())
        for i in range(0, len(all_public_keys), 100):
            validators = get_validators(
                beacon=self.beacon,
                public_keys=all_public_keys[i : i + 100],
                state_id="finalized",
            )
            for validator in validators:
                if validator["status"] in EXITED_STATUSES:
                    public_key = validator["validator"]["pubkey"]
                    result.add(public_key)

        return result

    @cached_property
    def vault_new_state(self) -> VaultState:
        """Calculates vault new state."""
        validator_keys_count: Dict[str, int] = collections.Counter(
            [
                keystore["validator_name"]
                for keystore in self.vault_current_state.values()
            ]
        )

        # allocate new validator clients if not enough capacity
        total_capacity = self.max_keys_per_validator * len(validator_keys_count)
        available_slots = (
            total_capacity
            - sum(validator_keys_count.values())
            - len(self.operator_exited_public_keys)
        )
        while available_slots < len(self.vault_missing_keypairs):
            new_validator_name = generate_validator_name(
                set(validator_keys_count.keys())
            )
            validator_keys_count[new_validator_name] = 0
            available_slots += self.max_keys_per_validator

        new_state = copy.deepcopy(self.vault_current_state)

        # get rid of exited validator keys
        for exited_public_key in self.operator_exited_public_keys:
            if exited_public_key in new_state:
                validator_name = self.vault_current_state[exited_public_key][
                    "validator_name"
                ]
                del new_state[exited_public_key]

                validator_keys_count[validator_name] -= 1

        # distribute missing keypairs across validators
        with click.progressbar(
            self.vault_missing_keypairs,
            label="Provisioning missing validator keys\t\t",
            show_percent=False,
            show_pos=True,
        ) as missing_keypairs:
            for public_key in missing_keypairs:
                validator_name = min(validator_keys_count, key=validator_keys_count.get)  # type: ignore
                if public_key not in new_state:
                    signing_key = self.vault_missing_keypairs[public_key]
                    secret = signing_key.key.to_bytes(32, "big")
                    password = self.get_or_create_keystore_password(validator_name)
                    keystore = ScryptKeystore.encrypt(
                        secret=secret, password=password, path=signing_key.path
                    ).as_json()
                    new_state[public_key] = VaultKeystore(
                        validator_name=validator_name, keystore=keystore
                    )
                    validator_keys_count[validator_name] += 1

        return new_state

    @lru_cache
    def get_or_create_keystore_password(self, validator_name) -> str:
        """Retrieves validator keystore password if exists or creates a new one."""
        try:
            password = self.vault_client.secrets.kv.read_secret(
                path=f"{validator_name}/password",
                mount_point=VAULT_VALIDATORS_MOUNT_POINT,
            )["data"]["password.txt"]
        except InvalidPath:
            password = generate_password()
            self.vault_client.secrets.kv.create_or_update_secret(
                path=f"{validator_name}/password",
                secret={"password.txt": password},
                mount_point=VAULT_VALIDATORS_MOUNT_POINT,
            )

        return password

    def apply_vault_changes(self) -> None:
        """Updates vault from current state to new state."""
        # update validator entries
        self.sync_vault_validators()

        # sync keystores
        self.sync_vault_keystores()

    def sync_vault_validators(self) -> None:
        """Synchronizes vault validators."""
        prev_validators: Set[str] = set(
            [
                keystore["validator_name"]
                for keystore in self.vault_current_state.values()
            ]
        )
        new_validators: Set[str] = set(
            [keystore["validator_name"] for keystore in self.vault_new_state.values()]
        )

        removed_validators = prev_validators.difference(new_validators)

        # sync validators
        with click.progressbar(
            length=len(new_validators) + len(removed_validators),
            label="Syncing vault validator directories\t\t",
            show_percent=False,
            show_pos=True,
        ) as bar:
            for validator_name in removed_validators:
                self.vault_client.sys.delete_policy(f"{self.namespace}-{validator_name}")
                self.vault_client.delete_kubernetes_role(f"{self.namespace}-{validator_name}")
                self.vault_client.secrets.kv.delete_secret(
                    path=f"{validator_name}/password",
                    mount_point=VAULT_VALIDATORS_MOUNT_POINT,
                )
                self.vault_client.secrets.kv.delete_secret(
                    path=f"{validator_name}/keystores",
                    mount_point=VAULT_VALIDATORS_MOUNT_POINT,
                )
                bar.update(1)

            for validator_name in new_validators:
                self.vault_client.sys.create_or_update_policy(
                    name=f"{self.namespace}-{validator_name}",
                    policy=VALIDATOR_POLICY
                    % (VAULT_VALIDATORS_MOUNT_POINT, validator_name),
                )
                self.vault_client.auth.kubernetes.create_role(
                    name=f"{self.namespace}-{validator_name}",
                    policies=[f"{self.namespace}-{validator_name}"],
                    bound_service_account_names=validator_name,
                    bound_service_account_namespaces=self.namespace,
                )
                bar.update(1)

    def sync_vault_keystores(self) -> None:
        """Synchronizes vault keystores."""
        validators_keystores: Dict[str, Dict[str, str]] = {}
        for public_key, vault_keystore in self.vault_new_state.items():
            validator_name = vault_keystore["validator_name"]
            keystores = validators_keystores.setdefault(validator_name, {})
            keystore = vault_keystore["keystore"]
            keystore_path = json.loads(keystore)["path"]

            if not keystore_path.endswith("/0/0"):
                index = keystore_path.split("/")[-1]
                keystore_path = f"m/{PURPOSE}/{COIN_TYPE}/{index}/0/0"

            # generate unique keystore name
            keystore_name = "keystore-%s-%i.json" % (
                keystore_path.replace("/", "_"),
                time.time(),
            )

            # save keystore
            keystores[keystore_name] = keystore

        # sync keystores in vault
        with click.progressbar(
            validators_keystores,
            label="Syncing vault keystores\t\t",
            show_percent=False,
            show_pos=True,
        ) as _validators_keystores:
            for validator_name in _validators_keystores:
                self.vault_client.secrets.kv.create_or_update_secret(
                    path=f"{validator_name}/keystores",
                    secret=validators_keystores[validator_name],
                    mount_point=VAULT_VALIDATORS_MOUNT_POINT,
                )

    def verify_vault_keystores(self) -> None:
        # clean up cached property
        try:
            # noinspection PyPropertyAccess
            del self.vault_validator_names
        except AttributeError:
            pass

        public_keys: Set[BLSPubkey] = set()
        with click.progressbar(
            self.vault_validator_names,
            label="Verifying vault state\t\t",
            show_percent=False,
            show_pos=True,
        ) as validator_names:
            for validator_name in validator_names:
                try:
                    validator_keystores: Dict[
                        str, str
                    ] = self.vault_client.secrets.kv.read_secret(
                        path=f"{validator_name}/keystores",
                        mount_point=VAULT_VALIDATORS_MOUNT_POINT,
                    )[
                        "data"
                    ]
                except InvalidPath:
                    continue

                for keystore_name, keystore_str in validator_keystores.items():
                    keystore = json.loads(keystore_str)
                    password = self.get_or_create_keystore_password(validator_name)
                    private_key = ScryptKeystore.from_json(keystore).decrypt(password)
                    public_key = G2ProofOfPossession.SkToPk(
                        int.from_bytes(private_key, byteorder="big")
                    )
                    if public_key in public_keys:
                        raise click.ClickException(
                            f"Public key {Web3.toHex(public_key)} is presented in multiple keystores"
                        )

                    public_keys.add(public_key)
                    if Web3.toBytes(hexstr=keystore["pubkey"]) != public_key:
                        # derived public key does not match the one in keystore
                        raise click.ClickException(
                            f"Failed to verify keystore {keystore_name} for validator {validator_name}"
                        )

    def check_mnemonic(self) -> None:
        """Checks whether the mnemonic is correct."""
        if not self.vault_current_state:
            return

        public_key1 = next(iter(self.vault_current_state))
        vault_keystore = self.vault_current_state[public_key1]
        keystore = ScryptKeystore.from_json(json.loads(vault_keystore["keystore"]))

        if not keystore.path.endswith("/0/0"):
            from_index = keystore.path.split("/")[-1]
        else:
            from_index = int(keystore.path.split("/")[3])

        signing_key = get_mnemonic_signing_key(
            mnemonic=self.mnemonic, from_index=from_index, is_legacy=IS_LEGACY
        )
        public_key2 = Web3.toHex(G2ProofOfPossession.SkToPk(signing_key.key))

        if public_key1 != public_key2:
            raise click.ClickException(
                "The vault keys does not belong to the provided mnemonic."
            )
