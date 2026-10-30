// SPDX-License-Identifier: MIT
// Fork Receipt v2: the deployed contract on the Portaldot V3 testnet's Revive pallet (EVM bytecode,
// accepted because Revive.AllowEVMBytecode is true there). It implements IForkReceipt.sol:
//
//   attest(bytes32 blockHash, bytes32 manifestRoot, uint64 keysLifted)
//     stores manifestRoot at slot blockHash and keysLifted at slot not(blockHash), and emits the
//     anonymous event Attested(bytes32 indexed blockHash, bytes32 manifestRoot, uint64 keysLifted, address attester)
//   receipt(bytes32 blockHash) view returns (bytes32 manifestRoot, uint64 keysLifted)
//     (0, 0) when nothing was attested for that block hash
//
// Written in Yul, not compiled from ForkReceipt.sol, because Revive charges a storage deposit per
// code byte (V3: 13.11 tPOT + 0.078 tPOT per byte, measured): the resolc (PolkaVM) build of
// ForkReceipt.sol needs 625.49 tPOT, the solc build 88.22 tPOT, this one fits a 29.5 tPOT funder.
// Size cuts, each a byte cost: the event is anonymous (no 33-byte signature topic), value transfers are
// not refused, and keysLifted is not range-checked (Forklift is the writer; it sends a u64).
// A later attest for the same block hash overwrites the earlier one; the events keep every attest.
object "ForkReceipt" {
    code {
        datacopy(0, dataoffset("runtime"), datasize("runtime"))
        return(0, datasize("runtime"))
    }
    object "runtime" {
        code {
            let blockHash := calldataload(4)
            switch shr(224, calldataload(0))
            case 0x7f86bc53 { // attest(bytes32,bytes32,uint64)
                let root := calldataload(36)
                let keys := calldataload(68)
                sstore(blockHash, root)
                sstore(not(blockHash), keys)
                mstore(0, root)
                mstore(32, keys)
                mstore(64, caller())
                log1(0, 96, blockHash) // anonymous event: topic 0 is the block hash
            }
            case 0x198a27aa { // receipt(bytes32)
                mstore(0, sload(blockHash))
                mstore(32, sload(not(blockHash)))
                return(0, 64)
            }
            default { revert(0, 0) }
        }
    }
}
