"""app/commands/run_billing_finalizer.py

CLI harness for finalize_call_billing() and refund_call_charge().

Usage
-----
  # Finalize a call (debit):
  python -m app.commands.run_billing_finalizer --call-id 42

  # Refund a call:
  python -m app.commands.run_billing_finalizer --call-id 42 --refund --reason-code test_refund

  # Finalize with a fault point (FLASK_ENV=testing only):
  FLASK_ENV=testing BILLING_FAULT_POINT=after_wallet_update \\
      python -m app.commands.run_billing_finalizer --call-id 42

  # Refund with a fault point:
  FLASK_ENV=testing BILLING_FAULT_POINT=after_refund_ledger_insert \\
      python -m app.commands.run_billing_finalizer --call-id 42 --refund --reason-code crash_test

Exit codes
----------
  0  — success (or already_finalized / already_refunded)
  1  — business-logic rejection (not_terminal, no_debit_row, etc.)
  2  — unexpected exception
  Abnormal termination via os._exit(1) when a fault point fires.

Fault points (FLASK_ENV=testing only)
--------------------------------------
Finalize:
  after_call_lock, after_charge_calculation, after_wallet_lock,
  after_wallet_update, after_ledger_insert, after_call_billing_update,
  before_transaction_commit

Refund:
  after_refund_wallet_lock, after_refund_wallet_update,
  after_refund_ledger_insert, after_refund_state_update, before_refund_commit
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_billing_finalizer",
        description="Finalize or refund a single call's billing (single atomic transaction).",
    )
    p.add_argument("--call-id", type=int, required=True, help="calls.id to process")
    p.add_argument(
        "--refund",
        action="store_true",
        default=False,
        help="Run refund_call_charge() instead of finalize_call_billing()",
    )
    p.add_argument(
        "--reason-code",
        default="manual_cli",
        help="reason_code passed to refund_call_charge() (default: manual_cli)",
    )
    p.add_argument(
        "--source",
        default="cli",
        help="source string passed to finalize/refund (default: cli)",
    )
    p.add_argument(
        "--fault-point",
        default=None,
        help=(
            "Set BILLING_FAULT_POINT to this value before importing billing_service. "
            "Only permitted when FLASK_ENV=testing. "
            "Equivalent to exporting BILLING_FAULT_POINT=<name> in the shell."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print result as JSON to stdout",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    # Apply --fault-point BEFORE importing billing_service so _init_fault_point() sees it.
    if args.fault_point:
        os.environ["BILLING_FAULT_POINT"] = args.fault_point

    # Bootstrap Flask app so db / config are available.
    try:
        from app import create_app  # type: ignore[import]
        app = create_app()
    except Exception as exc:
        print(f"ERROR: could not create Flask app: {exc}", file=sys.stderr)
        sys.exit(2)

    with app.app_context():
        try:
            if args.refund:
                from app.services.billing_service import refund_call_charge
                result = refund_call_charge(
                    args.call_id,
                    reason_code=args.reason_code,
                    source=args.source,
                )
                success_key = "refunded"
            else:
                from app.services.billing_service import finalize_call_billing
                result = finalize_call_billing(
                    args.call_id,
                    source=args.source,
                )
                success_key = "billed"
        except Exception as exc:
            msg = {"error": str(exc), "call_id": args.call_id}
            if args.json_output:
                print(json.dumps(msg))
            else:
                print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)

    # Serialise Decimal / int values for JSON.
    def _jsonify(v: object) -> object:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return str(v)
        return v

    serialisable = {k: _jsonify(v) for k, v in result.items()}

    if args.json_output:
        print(json.dumps(serialisable, indent=2))
    else:
        op = "refund" if args.refund else "finalize"
        print(f"[{op}] call_id={args.call_id}")
        for k, v in serialisable.items():
            print(f"  {k}: {v}")

    # Exit 0 on success or already-done; exit 1 on business rejection.
    succeeded = bool(result.get(success_key)) or bool(result.get("already_finalized")) or bool(result.get("already_refunded"))
    sys.exit(0 if succeeded else 1)


if __name__ == "__main__":
    main()
