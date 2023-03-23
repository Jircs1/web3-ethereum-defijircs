"""Anvil integration.

- `Anvil <https://github.com/foundry-rs/foundry/blob/master/anvil/README.md>`__
  is a blazing-fast local testnet node implementation in Rust.

- Anvil can replace :py:class:`eth_tester.main.EthereumTester` as the unit/integration test backend.

- Anvil is mostly used in mainnet fork test cases.

- Anvil is a more stable an alternative to Ganache (:py:mod:`eth_defi.ganache`)

- Anvil is part of `Foundry <https://github.com/foundry-rs/foundry>`__,
  a toolkit for Ethereum application development.

To install Anvil on your UNIX computer:

.. code-block:: shell

    curl -L https://foundry.paradigm.xyz | bash
    PATH=~/.foundry/bin:$PATH
    foundryup  # Needs to be in path, or installation fails

This will install `foundryup`, `anvil` at `~/.foundry/bin` and adds the folder to your shell rc file `PATH`.

For more information see `Anvil reference <https://book.getfoundry.sh/reference/anvil/>`__.

See also :py:mod:`eth_defi.trace` for Solidity tracebacks using Anvil.

This code was originally lifted from Brownie project.
"""

import logging
import sys
import time
import warnings
from dataclasses import dataclass
from subprocess import DEVNULL, PIPE
from typing import Dict, List, Optional, Union, Tuple, Any

import psutil
import requests
from eth_typing import HexAddress
from requests.exceptions import ConnectionError as RequestsConnectionError
from web3 import HTTPProvider, Web3

from eth_defi.utils import is_localhost_port_listening, shutdown_hard

logger = logging.getLogger(__name__)


class InvalidArgumentWarning(Warning):
    """Lifted from Brownie."""


class RPCRequestError(Exception):
    """Lifted from Brownie."""


#: Mappings between Anvil command line parameters and our internal argument names
CLI_FLAGS = {
    "port": "--port",
    "host": "--host",
    "fork": "--fork-url",
    "fork_block": "--fork-block-number",
    "hardfork": "--hardfork",
    "chain_id": "--chain-id",
    "default_balance": "--balance",
    "gas_limit": "--gas-limit",
    "block_time": "--block-time",
    "steps_tracing": "--steps-tracing",
}


def _launch(cmd: str, **kwargs) -> Tuple[psutil.Popen, List[str]]:
    """Launches the RPC client.

    Args:
        cmd: command string to execute as subprocess"""
    if sys.platform == "win32" and not cmd.split(" ")[0].endswith(".cmd"):
        if " " in cmd:
            cmd = cmd.replace(" ", ".cmd ", 1)
        else:
            cmd += ".cmd"
    cmd_list = cmd.split(" ")
    for key, value in [(k, v) for k, v in kwargs.items() if v]:
        try:
            if value is True or value is False:
                # GNU style flags like --step-tracing
                if value:
                    cmd_list.append(CLI_FLAGS[key])
            else:
                cmd_list.extend([CLI_FLAGS[key], str(value)])
        except KeyError:
            warnings.warn(
                f"Ignoring invalid commandline setting for anvil: " f'"{key}" with value "{value}".',
                InvalidArgumentWarning,
            )
    final_cmd_str = " ".join(cmd_list)
    logger.info("Launching anvil: %s", final_cmd_str)
    out = DEVNULL if sys.platform == "win32" else PIPE

    return psutil.Popen(cmd_list, stdin=DEVNULL, stdout=out, stderr=out), cmd_list


def make_anvil_custom_rpc_request(web3: Web3, method: str, args: Optional[List] = None) -> Any:
    """Make a request to special named EVM JSON-RPC endpoint.

    - `See the Anvil custom RPC methods here <https://book.getfoundry.sh/reference/anvil/>`__.

    :param method:
        RPC endpoint name

    :param args:
        JSON-RPC call arguments

    :return:
        RPC result

    :raise RPCRequestError:
        In the case RPC method errors
    """

    if args is None:
        args = ()

    try:
        response = web3.provider.make_request(method, args)  # type: ignore
        if "result" in response:
            return response["result"]

    except (AttributeError, RequestsConnectionError):
        raise RPCRequestError("Web3 is not connected.")

    raise RPCRequestError(response["error"]["message"])


@dataclass
class AnvilLaunch:
    """Control Anvil processes launched on background.

    Comes with a helpful :py:meth:`close` method when it is time to put Anvil rest.
    """

    #: Which port was bound by the Anvil
    port: int

    #: Used command-line to spin up anvil
    cmd: List[str]

    #: Where does Anvil listen to JSON-RPC
    json_rpc_url: str

    #: UNIX process that we opened
    process: psutil.Popen

    def close(self, log_level: Optional[int] = None, block=True, block_timeout=30) -> Tuple[bytes, bytes]:
        """Close the background Anvil process.

        :param log_level:
            Dump Anvil messages to logging

        :param block:
            Block the execution until anvil is gone

        :param block_timeout:
            How long time we try to kill Anvil until giving up.

        :return:
            Anvil stdout, stderr as string
        """
        stdout, stderr = shutdown_hard(
            self.process,
            log_level=log_level,
            block=block,
            block_timeout=block_timeout,
            check_port=self.port,
        )
        logger.info("Anvil shutdown %s", self.json_rpc_url)
        return stdout, stderr


def launch_anvil(
    fork_url: Optional[str] = None,
    unlocked_addresses: List[Union[HexAddress, str]] = None,
    cmd="anvil",
    port: int = 19999,
    block_time=0,
    launch_wait_seconds=20.0,
    attempts=3,
    hardfork="london",
    gas_limit: Optional[int] = None,
    steps_tracing=False,
) -> AnvilLaunch:
    """Creates Anvil unit test backend or mainnet fork.

    - Anvil can be used as web3.py test backend instead of `EthereumTester`.
      Anvil offers faster execution and tracing - see :py:mod:`eth_defi.trace`.

    - Forking a mainnet is a common way to test against live deployments.
      This function invokes `anvil` command and tells it to fork a given JSON-RPC endpoint.

    When called, a subprocess is started on the background.
    To stop this process, call :py:meth:`eth_defi.anvil.AnvilLaunch.close`.

    This function waits `launch_wait_seconds` in order to `anvil` process to start
    and complete the chain fork.

    **Unit test backend**:

    - See `eth_defi.tests.enzyme.conftest <https://github.com/tradingstrategy-ai/web3-ethereum-defi/blob/master/tests/enzyme/conftest.py>`__ for an example
      how to use Anvil in your Python based unit test suite

    **Mainnet fork**: Here is an example that forks BNB chain mainnet and transfer 500 BUSD stablecoin to a test
    account we control:

    .. code-block:: python

        from eth_defi.anvil import fork_network_anvil
        from eth_defi.chain import install_chain_middleware
        from eth_defi.gas import node_default_gas_price_strategy

        @pytest.fixture()
        def large_busd_holder() -> HexAddress:
            # An onchain address with BUSD balance
            # Binance Hot Wallet 6
            return HexAddress(HexStr("0x8894E0a0c962CB723c1976a4421c95949bE2D4E3"))


        @pytest.fixture()
        def user_1() -> LocalAccount:
            # Create a test account
            return Account.create()


        @pytest.fixture()
        def anvil_bnb_chain_fork(request, large_busd_holder, user_1, user_2) -> str:
             # Create a testable fork of live BNB chain.
            mainnet_rpc = os.environ["BNB_CHAIN_JSON_RPC"]
            launch = fork_network_anvil(mainnet_rpc, unlocked_addresses=[large_busd_holder])
            try:
                yield launch.json_rpc_url
            finally:
                # Wind down Anvil process after the test is complete
                launch.close(log_level=logging.ERROR)


        @pytest.fixture()
        def web3(anvil_bnb_chain_fork: str):
            # Set up a local unit testing blockchain
            # https://web3py.readthedocs.io/en/stable/examples.html#contract-unit-tests-in-python
            web3 =  Web3(HTTPProvider(anvil_bnb_chain_fork))
            # Anvil needs POA middlware if parent chain needs POA middleware
            install_chain_middleware(web3)
            web3.eth.set_gas_price_strategy(node_default_gas_price_strategy)
            return web3

        def test_anvil_fork_transfer_busd(web3: Web3, large_busd_holder: HexAddress, user_1: LocalAccount):
            # Forks the BNB chain mainnet and transfers from USDC to the user.

            # BUSD deployment on BNB chain
            # https://bscscan.com/token/0xe9e7cea3dedca5984780bafc599bd69add087d56
            busd_details = fetch_erc20_details(web3, "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56")
            busd = busd_details.contract

            # Transfer 500 BUSD to the user 1
            tx_hash = busd.functions.transfer(user_1.address, 500 * 10**18).transact({"from": large_busd_holder})

            # Because Ganache has instamine turned on by default, we do not need to wait for the transaction
            receipt = web3.eth.get_transaction_receipt(tx_hash)
            assert receipt.status == 1, "BUSD transfer reverted"

            assert busd.functions.balanceOf(user_1.address).call() == 500 * 10**18

    `See the full example in tests source code <https://github.com/tradingstrategy-ai/web3-ethereum-defi/blob/master/tests/test_anvil.py>`_.

    If `anvil` refuses to terminate properly, you can kill a process by a port in your terminal:

    .. code-block:: shell

        # Kill any process listening to localhost:19999
        kill -SIGKILL $(lsof -ti:19999)

    See also

    - :py:func:`eth_defi.trace.assert_transaction_success_with_explanation`

    - :py:func:`eth_defi.trace.print_symbolic_trace`

    :param cmd:
        Override `anvil` command. If not given we look up from `PATH`.

    :param fork_url:
        HTTP JSON-RPC URL of the network we want to fork.

        If not given launch an empty test backend.

    :param unlocked_addresses:
        List of addresses of which ownership we take to allow test code to transact as them

    :param port:
        Localhost port we bind for Anvil JSON-RPC

    :param launch_wait_seconds:
        How long we wait anvil to start until giving up

    :param block_time:

        How long Anvil takes to mine a block. Default is zero:
        Anvil is in `automining mode <https://book.getfoundry.sh/reference/anvil/>`__
        and creates a new block for each new transaction.

        Set to `1` or higher so that you can poll the transaction as you would do with
        a live JSON-RPC node.

    :param attempts:
        How many attempts we do to start anvil.

        Anvil launch may fail without any output. This could be because the given JSON-RPC
        node is throttling your API requests. In this case we just try few more times
        again by killing the Anvil process and starting it again.

    :param gas_limit:
        Set the block gas limit.

    :param hardfork:
        EVM version to use

    :param step_tracing:
        Enable Anvil step tracing.

        Needed to get structured logs.

        Only needed on GoEthereum style tracing, not needed for Parity style tracing.

        See https://book.getfoundry.sh/reference/anvil/

    """

    assert not is_localhost_port_listening(port), f"localhost port {port} occupied.\n" f"You might have a zombie Anvil process around.\nRun to kill: -SIGKILL $(lsof -ti:{port})"

    url = f"http://localhost:{port}"

    attempts_left = attempts
    process = None
    final_cmd = None
    current_block = 0
    web3 = None

    if unlocked_addresses is None:
        unlocked_addresses = []

    # https://book.getfoundry.sh/reference/anvil/
    args = dict(
        port=port,
        fork=fork_url,
        hardfork=hardfork,
        gas_limit=gas_limit,
        steps_tracing=steps_tracing,
    )

    if block_time not in (0, None):
        assert block_time > 0, f"Got bad block time {block_time}"
        args["block_time"] = block_time

    while attempts_left > 0:

        process, final_cmd = _launch(
            cmd,
            **args,
        )

        # Wait until Anvil is responsive
        timeout = time.time() + launch_wait_seconds

        # Use short 1.0s HTTP read timeout here - otherwise requests will wa-it > 10s if something is wrong

        web3 = Web3(HTTPProvider(url, request_kwargs={"timeout": 1.0}))
        while time.time() < timeout:

            if process.poll() is not None:
                # Anvil crashed - hopefully got some output
                raise AssertionError(f"anvil process did not start up: {final_cmd}")

            try:
                # See if web3 RPC works
                current_block = web3.eth.block_number
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
                logger.info("Anvil not ready, got exception %s", e)
                # requests.exceptions.ConnectionError: ('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))
                time.sleep(0.1)
                continue

        if current_block is None:
            logger.error("Could not read the latest block from anvil %s within %f seconds, shutting down and dumping output", url, launch_wait_seconds)
            stdout, stderr = shutdown_hard(
                process,
                log_level=logging.ERROR,
                block=True,
                check_port=port,
            )

            if len(stdout) == 0:
                attempts_left -= 1
                if attempts_left > 0:
                    logger.info("anvil did not start properly, try again, attempts left %d", attempts_left)
                    continue

            raise AssertionError(f"Could not read block number from Anvil after the launch {cmd}: at {url}, stdout is {len(stdout)} bytes, stderr is {len(stderr)} bytes")
        else:
            # We have a successful launch
            break

    chain_id = web3.eth.chain_id

    # Use f-string for a thousand separator formatting
    logger.info(f"anvil forked network {chain_id}, the current block is {current_block:,}, Anvil JSON-RPC is {url}")

    # Perform unlock accounts for all accounts
    for account in unlocked_addresses:
        unlock_account(web3, account)

    return AnvilLaunch(port, final_cmd, url, process)


def unlock_account(web3: Web3, address: str):
    """Make Anvil mainnet fork to accept transactions to any Ethereum account.

    This is even when we do not have a private key for the account.

    :param web3:
        Web3 instance

    :param address:
        Account to unlock
    """
    web3.provider.make_request("anvil_impersonateAccount", [address])  # type: ignore


def sleep(web3: Web3, seconds: int) -> int:
    """Call emv_increaseTime on Anvil"""
    make_anvil_custom_rpc_request(web3, "evm_increaseTime", [hex(seconds)])
    return seconds


def mine(web3: Web3, timestamp: Optional[int] = None) -> None:
    """Call evm_setNextBlockTimestamp on Anvil"""

    if timestamp is None:
        block = web3.eth.get_block(web3.eth.block_number)
        timestamp = block["timestamp"] + 1

    make_anvil_custom_rpc_request(web3, "evm_mine", [timestamp])


def snapshot(web3: Web3) -> int:
    """Call evm_snapshot on Anvil"""
    return make_anvil_custom_rpc_request(web3, "evm_snapshot", [])


def revert(web3: Web3, snapshot_id: int) -> None:
    """Call evm_revert on Anvil"""
    make_anvil_custom_rpc_request(web3, "evm_revert", [snapshot_id])


# Backwards compatibility
fork_network_anvil = launch_anvil