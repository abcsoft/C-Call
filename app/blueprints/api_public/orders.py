from __future__ import annotations

import logging
from flask import jsonify, request, g
from app.blueprints.api_public import api_bp
from app.blueprints.auth.decorators import require_api_key, require_api_scope
from app.services import order_service
from app.exceptions import InsufficientBalanceError

logger = logging.getLogger("ccall")

@api_bp.route("/order-call", methods=["POST"])
@api_bp.route("/orders", methods=["POST"])
@require_api_key
@require_api_scope("orders:write")
def api_create_order():
    """Receive order details from merchant and queue a confirmation call.
    Uses Submission Idempotency: duplicate external order IDs return the existing order.
    """
    data = request.get_json(silent=True) or {}
    tenant_id = g.tenant_id

    try:
        order = order_service.create_order(tenant_id, data)
        # Fetch attempt details if any
        attempts = order.get("attempts") or order_service.get_call_attempts_for_order(tenant_id, order["id"])
        latest_call_id = attempts[-1]["call_id"] if attempts else None
        
        return jsonify({
            "success": True,
            "order_id": order["external_order_id"],
            "internal_id": order["id"],
            "status": order["order_status"],
            "call_id": latest_call_id,
            "message": "Confirmation call queued successfully."
        }), 200
    except ValueError as val_err:
        logger.warning(f"Validation error on order creation: {val_err}, data: {data}")
        return jsonify({
            "success": False,
            "error": str(val_err),
            "code": "VALIDATION_ERROR"
        }), 400
    except InsufficientBalanceError as bal_err:
        return jsonify({
            "success": False,
            "error": str(bal_err),
            "code": "INSUFFICIENT_BALANCE",
            "recharge_url": "/wallet"
        }), 402
    except Exception as exc:
        logger.error(f"Internal API error on order creation for tenant {tenant_id}: {exc}")
        return jsonify({
            "success": False,
            "error": "An unexpected error occurred while placing the order.",
            "code": "INTERNAL_SERVER_ERROR"
        }), 500

@api_bp.route("/orders/<string:external_id>", methods=["GET"])
@require_api_key
@require_api_scope("orders:read")
def api_get_order(external_id: str):
    """Retrieve details and call history of a specific order by ID or external ID."""
    tenant_id = g.tenant_id
    order = order_service.get_order(external_id, tenant_id)
    if not order:
        return jsonify({
            "success": False,
            "error": f"Order with ID {external_id} not found.",
            "code": "NOT_FOUND"
        }), 404

    return jsonify({
        "success": True,
        "data": {
            "id": order["id"],
            "external_order_id": order["external_order_id"],
            "phone": order["phone"],
            "customer_name": order["customer_name"],
            "amount": float(order["amount"] or 0),
            "order_status": order["order_status"],
            "attempt_count": order["attempt_count"],
            "max_attempts": order["max_attempts"],
            "next_retry_at": order["next_retry_at"].isoformat() if order.get("next_retry_at") else None,
            "confirmed_at": order["confirmed_at"].isoformat() if order.get("confirmed_at") else None,
            "caller_id": order["caller_id"],
            "webhook_status": order["webhook_status"],
            "metadata": order.get("metadata") or {},
            "custom_text": order.get("custom_text"),
            "custom_audio_url": order.get("custom_audio_url"),
            "created_at": order["created_at"].isoformat(),
            "attempts": [{
                "id": att["id"],
                "attempt_no": att["attempt_no"],
                "call_id": att["call_id"],
                "result": att["result"],
                "dtmf_digit": att["dtmf_digit"],
                "talk_seconds": att["talk_seconds"],
                "charge": float(att["charge"] or 0),
                "started_at": att["started_at"].isoformat() if att.get("started_at") else None,
                "ended_at": att["ended_at"].isoformat() if att.get("ended_at") else None
            } for att in order.get("attempts", [])]
        }
    }), 200

@api_bp.route("/orders", methods=["GET"])
@require_api_key
@require_api_scope("orders:read")
def api_list_orders():
    """List orders, support filtering by status, search, and date range."""
    tenant_id = g.tenant_id
    
    status = request.args.get("status")
    search = request.args.get("search")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 20))))
    except ValueError:
        page = 1
        per_page = 20

    filters = {
        "status": status,
        "search": search,
        "date_from": date_from,
        "date_to": date_to
    }
    
    res = order_service.list_orders(tenant_id, filters=filters, page=page, per_page=per_page)

    return jsonify({
        "success": True,
        "data": [{
            "id": ord["id"],
            "external_order_id": ord["external_order_id"],
            "phone": ord["phone"],
            "customer_name": ord["customer_name"],
            "amount": float(ord["amount"] or 0),
            "order_status": ord["order_status"],
            "created_at": ord["created_at"].isoformat()
        } for ord in res["data"]],
        "total": res["total"],
        "page": page,
        "per_page": per_page
    }), 200

@api_bp.route("/orders/<string:external_id>/recall", methods=["POST"])
@require_api_key
@require_api_scope("orders:write")
def api_recall_order(external_id: str):
    """Manually trigger a recall for an order."""
    tenant_id = g.tenant_id
    data = request.get_json(silent=True) or {}
    
    # Try to find the order
    order = order_service.get_order_by_external_id_service(tenant_id, external_id)
    if not order:
        # If not found but data is provided, recreate/create it!
        if data:
            try:
                order = order_service.create_order(tenant_id, data)
                # Retrieve the latest call attempt's call_id
                attempts = order_service.get_call_attempts_for_order(tenant_id, order["id"])
                call_id = attempts[-1]["call_id"] if attempts else None
                return jsonify({
                    "success": True,
                    "call_id": call_id,
                    "message": "Order created and call placed in queue."
                }), 200
            except Exception as e:
                return jsonify({
                    "success": False,
                    "error": f"Failed to create order on recall: {str(e)}",
                    "code": "CREATE_FAILED"
                }), 400
        else:
            return jsonify({
                "success": False,
                "error": f"Order with ID {external_id} not found and no payload provided.",
                "code": "NOT_FOUND"
            }), 404
            
    # Order exists, proceed with normal manual_recall
    try:
        call_id = order_service.manual_recall(order["id"], tenant_id)
        return jsonify({
            "success": True,
            "call_id": call_id,
            "message": "Manual re-call placed in queue."
        }), 200
    except ValueError as val_err:
        return jsonify({
            "success": False,
            "error": str(val_err),
            "code": "NOT_FOUND"
        }), 404
    except InsufficientBalanceError as bal_err:
        return jsonify({
            "success": False,
            "error": str(bal_err),
            "code": "INSUFFICIENT_BALANCE"
        }), 402
    except Exception as exc:
        logger.error(f"Internal API error on manual recall for order {external_id}: {exc}")
        return jsonify({
            "success": False,
            "error": "An unexpected error occurred while placing the call.",
            "code": "INTERNAL_SERVER_ERROR"
        }), 500
