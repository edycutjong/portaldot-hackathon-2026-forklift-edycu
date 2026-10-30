// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/// @title Fork Receipt in plain Solidity: the comparison build, NOT deployed
/// @notice scripts/revive_deposits.py measures the V3 Revive deposit of this contract compiled with resolc
///         (PolkaVM) and with solc (EVM bytecode). Both exceeded the funder's balance, so the deployed
///         Receipt v2 is forklift/contracts/ForkReceipt.yul (interface IForkReceipt.sol).
contract ForkReceipt {
    struct Receipt {
        bytes32 blockHash;
        bytes32 manifestRoot;
        uint64 keysLifted;
        uint64 attestedAt; // block number on the chain that holds this contract
        address attester;
    }

    Receipt[] private receipts;

    event Attested(
        uint256 indexed id,
        bytes32 indexed blockHash,
        bytes32 manifestRoot,
        uint64 keysLifted,
        address attester
    );

    error NoSuchReceipt(uint256 id);

    /// @notice Store one receipt and emit it. Anyone may attest; the attester is recorded.
    function attest(bytes32 blockHash, bytes32 manifestRoot, uint64 keysLifted) external returns (uint256 id) {
        id = receipts.length;
        receipts.push(Receipt(blockHash, manifestRoot, keysLifted, uint64(block.number), msg.sender));
        emit Attested(id, blockHash, manifestRoot, keysLifted, msg.sender);
    }

    function count() external view returns (uint256) {
        return receipts.length;
    }

    function receipt(uint256 id) external view returns (Receipt memory) {
        if (id >= receipts.length) revert NoSuchReceipt(id);
        return receipts[id];
    }
}
