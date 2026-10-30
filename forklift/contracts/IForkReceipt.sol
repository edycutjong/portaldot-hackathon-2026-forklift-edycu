// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/// @title Fork Receipt v2 (Revive) — the interface ForkReceipt.yul implements
/// @notice A public record of which chain state a Forklift fork carries: the pinned block hash,
///         the root of the lift manifest and the number of keys lifted. v1 is `receipt.wat` on the
///         fork's own Contracts pallet; v2 lives on the Portaldot V3 testnet's Revive pallet.
interface IForkReceipt {
    event Attested(bytes32 indexed blockHash, bytes32 manifestRoot, uint64 keysLifted, address attester)
        anonymous;

    /// @notice Store manifestRoot and keysLifted under blockHash (a later attest overwrites) and emit Attested.
    function attest(bytes32 blockHash, bytes32 manifestRoot, uint64 keysLifted) external;

    /// @notice The stored receipt for blockHash; (0, 0) when nothing was attested for it or for its bitwise
    ///         complement (the two storage slots of an attested h are h and not(h)). The event from a known
    ///         attester is the record to trust: anyone may attest, and a later attest overwrites.
    function receipt(bytes32 blockHash) external view returns (bytes32 manifestRoot, uint64 keysLifted);
}
