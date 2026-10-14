;; Forklift Fork Receipt: a contract for the rent-era Contracts pallet of Portaldot spec 1002.
;; seal0 ABI only, hand-written, so it needs no Rust toolchain (ink! 5 needs seal1/seal2,
;; which spec 1002 does not have).
;;
;; deploy and call both run attest(input):
;;   input = mainnet_block_hash (32 B) ‖ manifest_root (32 B) ‖ keys_lifted (u64, little-endian)
;;   emits Contracts.ContractEmitted(<this contract>, input), the 72 input bytes unchanged.
;; Any other input length traps, so a malformed attest fails instead of emitting.
;; Build: wat2wasm receipt.wat -o receipt.wasm
(module
  (import "seal0" "seal_input" (func $seal_input (param i32 i32)))
  (import "seal0" "seal_deposit_event" (func $seal_deposit_event (param i32 i32 i32 i32)))
  (import "env" "memory" (memory 1 1))

  ;; Memory: [0, 4) the input-length cell seal_input reads and writes; [32, 160) the input buffer.
  (func $attest
    (i32.store (i32.const 0) (i32.const 128))
    (call $seal_input (i32.const 32) (i32.const 0))
    (if (i32.ne (i32.load (i32.const 0)) (i32.const 72))
      (then (unreachable)))
    ;; no topics (pointer and length 0), data = the 72 input bytes
    (call $seal_deposit_event (i32.const 0) (i32.const 0) (i32.const 32) (i32.const 72)))

  (func (export "deploy") (call $attest))
  (func (export "call") (call $attest)))
